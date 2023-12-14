#  Copyright 2023 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.


import numpy as np
from packaging import version
import openvino as ov
from openvino.runtime import opset13
from optimum.intel.utils.import_utils import is_openvino_version


def model_has_name(ov_model: ov.Model, name: str):
    return name in sum([list(t.get_names()) for t in ov_model.inputs + ov_model.outputs], list())


def model_has_input(ov_model: ov.Model, name: str):
    return name in sum([list(t.get_names()) for t in ov_model.inputs], list())


def model_has_cache_reorder(ov_model):
    return model_has_input(ov_model, 'beam_idx')


def model_has_state(ov_model):
    # TODO: Provide a better way based on the variables availability, but OV Python API doesn't expose required methods
    return len(ov_model.get_sinks()) > 0


def fuse_cache_reorder(ov_model: ov.Model, not_kv_inputs, key_value_input_names, gather_dim: int):
    """ Adds a new beam_idx parameter and Gather op per each kv-cache input in a given model.
        Should be run before make_stateful. Implements optimumum's _reorder_cache
        inside the model in the beginning of each iteration.
        Gather works along given gather_dim dimension that may vary from model to model.
        KV-cache inputs are identified based on names in key_value_input_names.
        Append the new beam_idx parameter to not_kv_inputs.
    """

    assert not model_has_name(ov_model, 'beam_idx')
    input_batch = ov_model.input('input_ids').get_partial_shape()[0]
    beam_idx = opset13.parameter(name='beam_idx', dtype=ov.Type.i32, shape=ov.PartialShape([input_batch]))
    beam_idx.output(0).get_tensor().add_names({'beam_idx'})  # why list is not accepted?
    ov_model.add_parameters([beam_idx])
    not_kv_inputs.append(ov_model.inputs[-1])
    # Go over all cache parameters and fuse _reorder_cache with indices provided by the new parameter beam_idx
    for input_name in key_value_input_names:
        parameter_output_port = ov_model.input(input_name)
        consumers = parameter_output_port.get_target_inputs()
        gather = opset13.gather(parameter_output_port, beam_idx, opset13.constant(gather_dim))
        for consumer in consumers:
            consumer.replace_source_output(gather.output(0))
    ov_model.validate_nodes_and_infer_types()


def build_state_initializer(ov_model: ov.Model, batch_dim):
    """Build initialization ShapeOf Expression for all ReadValue ops"""
    input_ids = ov_model.input('input_ids')
    batch = opset13.gather(opset13.shape_of(input_ids, output_type='i64'), opset13.constant([0]), opset13.constant(0))
    for op in ov_model.get_ops():
        if op.get_type_name() == 'ReadValue':
            dims = [dim.min_length for dim in list(op.get_output_partial_shape(0))]
            dims[batch_dim] = batch
            dims = [opset13.constant(np.array([dim], dtype=np.int64)) if type(dim) is int else dim for dim in dims]
            shape = opset13.concat(dims, axis=0)
            broadcast = opset13.broadcast(opset13.constant(0.0, dtype=op.get_output_element_type(0)), shape)
            op.set_arguments([broadcast])
    ov_model.validate_nodes_and_infer_types()


def make_stateful(
        ov_model: ov.Model,
        not_kv_inputs,
        key_value_input_names,
        key_value_output_names,
        batch_dim,
        num_attention_heads,
        num_beams_and_batch=None):
    """ Hides kv-cache inputs and outputs inside the model as variables.
    """
    from openvino._offline_transformations import apply_make_stateful_transformation

    input_output_map = {}
    # TODO: Can we derive the dimensions from the model topology?

    if num_beams_and_batch is not None:
        # Set batch size for input_ids and attention mask to avoid dynamic dimension got propagated from the end of the model back to ReadValue
        for input in not_kv_inputs:
            shape = input.get_partial_shape()
            if shape.rank.get_length() <= 2:  # == 1 for beam_index
                shape[0] = num_beams_and_batch
                input.get_node().set_partial_shape(shape)
            else:
                print(f'[ WARNING ] Rank of {input.get_any_name()} input of the model is not 2, batch size is not set')

    for kv_name_pair in zip(key_value_input_names, key_value_output_names):
        input_output_map[kv_name_pair[0]] = kv_name_pair[1]
        if num_beams_and_batch is not None:
            input = ov_model.input(kv_name_pair[0])
            shape = input.get_partial_shape()
            shape[batch_dim] = num_beams_and_batch * num_attention_heads
            input.get_node().set_partial_shape(shape)

    if num_beams_and_batch is not None:
        # Re-validation model if shapes are altered above
        ov_model.validate_nodes_and_infer_types()

    apply_make_stateful_transformation(ov_model, input_output_map)
    if num_beams_and_batch is None:
        build_state_initializer(ov_model, batch_dim)


def raise_if_openvino_is_too_old():
    if is_openvino_version("<=", "2023.2"):
        raise ValueError(f'Could not create or use stateful model when using old version of openvino=={ov.__version__}. Install openvino>=2023.3.0.')


def patch_stateful(model, ov_model):
    raise_if_openvino_is_too_old()
    not_kv_inputs = [input for input in ov_model.inputs if not any(name in model.key_value_input_names for name in input.get_names())]

    # By default, batch is the 0-th but chatglm uses 1-st dimension as batch
    # TODO: Deduce from a model via ordinal reshape (?) and topology
    batch_dim = 1 if model.config.model_type == 'chatglm' else 0

    fuse_cache_reorder(ov_model, not_kv_inputs, model.key_value_input_names, batch_dim)

    num_attention_heads = model.normalized_config.num_attention_heads if model.config.model_type == 'bloom' else 1

    make_stateful(
        ov_model,
        not_kv_inputs,
        model.key_value_input_names,
        model.key_value_output_names,
        batch_dim,
        num_attention_heads,
        None)