#!/usr/bin/env python3

# Copyright 2019-2025, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import argparse
import os

import numpy as np
from gen_common import (
    np_to_model_dtype,
    np_to_onnx_dtype,
    np_to_torch_dtype,
    np_to_trt_dtype,
    openvino_save_model,
)

FLAGS = None
np_dtype_string = np.dtype(object)


def create_plan_shape_tensor_modelfile(
    models_dir, model_version, max_batch, dtype, shape, shape_tensor_input_dtype
):
    # Note that resize layer does not support int tensors.
    # The model takes three inputs (INPUT, DUMMY_INPUT and SHAPE_INPUT)
    # and four control inputs(START, END, READY, CORR_ID).
    # In absence of proper accumulator,
    # OUTPUT : 0 if not-ready and 'DUMMY_INPUT'+'START'+('END'*'CORRID')
    #          otherwise
    # RESIZED_OUTPUT : Obtained after resizing 'INPUT' to shape specified
    #          in 'SHAPE_INPUT'
    # SHAPE_OUTPUT : The shape values of resized output

    trt_dtype = np_to_trt_dtype(dtype)
    trt_shape_dtype = np_to_trt_dtype(shape_tensor_input_dtype)
    trt_memory_format = trt.TensorFormat.LINEAR

    TRT_LOGGER = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(TRT_LOGGER)
    network = builder.create_network()

    unit_shape = [1] * len(shape)
    dummy_shape = [-1] * len(shape)
    if max_batch != 0:
        in0 = network.add_input("INPUT", trt.int32, [-1] + dummy_shape)
        dummy_in0 = network.add_input("DUMMY_INPUT", trt_dtype, [-1] + dummy_shape)
        shape_in0 = network.add_input("SHAPE_INPUT", trt_shape_dtype, [1 + len(shape)])
        start0 = network.add_input("START", trt.int32, [-1] + unit_shape)
        end0 = network.add_input("END", trt.int32, [-1] + unit_shape)
        ready0 = network.add_input("READY", trt.int32, [-1] + unit_shape)
        corrid0 = network.add_input("CORRID", trt.int32, [-1] + unit_shape)
    else:
        in0 = network.add_input("INPUT", trt.int32, dummy_shape)
        dummy_in0 = network.add_input("DUMMY_INPUT", trt_dtype, dummy_shape)
        shape_in0 = network.add_input("SHAPE_INPUT", trt_shape_dtype, [len(shape)])
        start0 = network.add_input("START", trt.int32, unit_shape)
        end0 = network.add_input("END", trt.int32, unit_shape)
        ready0 = network.add_input("READY", trt.int32, unit_shape)
        corrid0 = network.add_input("CORRID", trt.int32, unit_shape)

    add0 = network.add_elementwise(in0, start0, trt.ElementWiseOperation.SUM)
    mul0 = network.add_elementwise(end0, corrid0, trt.ElementWiseOperation.PROD)
    sum0 = network.add_elementwise(
        add0.get_output(0), mul0.get_output(0), trt.ElementWiseOperation.SUM
    )
    out0 = network.add_elementwise(
        sum0.get_output(0), ready0, trt.ElementWiseOperation.PROD
    ).get_output(0)

    resize_layer = network.add_resize(dummy_in0)
    resize_layer.set_input(1, shape_in0)
    shape_out0 = network.add_shape(resize_layer.get_output(0))
    resized_out0 = resize_layer.get_output(0)

    shape_out0.get_output(0).name = "SHAPE_OUTPUT"
    shape_out0.get_output(0).dtype = trt.int64
    network.mark_output_for_shapes(shape_out0.get_output(0))

    out0.name = "OUTPUT"
    out0.dtype = trt.int32
    network.mark_output(out0)

    resized_out0.name = "RESIZED_OUTPUT"
    resized_out0.dtype = trt_dtype
    network.mark_output(resized_out0)

    shape_in0.allowed_formats = 1 << int(trt_memory_format)
    dummy_in0.allowed_formats = 1 << int(trt_memory_format)
    start0.allowed_formats = 1 << int(trt_memory_format)
    ready0.allowed_formats = 1 << int(trt_memory_format)
    out0.allowed_formats = 1 << int(trt_memory_format)
    shape_out0.get_output(0).allowed_formats = 1 << int(trt_memory_format)
    resized_out0.allowed_formats = 1 << int(trt_memory_format)

    if trt_dtype == trt.int8:
        dummy_in0.dynamic_range = (-128.0, 127.0)
        resized_out0.dynamic_range = (-128.0, 127.0)
        start0.dynamic_range = (-128.0, 127.0)
        end0.dynamic_range = (-128.0, 127.0)
        ready0.dynamic_range = (-128.0, 127.0)

    flags = 1 << int(trt.BuilderFlag.DIRECT_IO)
    flags |= 1 << int(trt.BuilderFlag.PREFER_PRECISION_CONSTRAINTS)
    flags |= 1 << int(trt.BuilderFlag.REJECT_EMPTY_ALGORITHMS)

    if trt_dtype == trt.int8:
        flags |= 1 << int(trt.BuilderFlag.INT8)
    elif trt_dtype == trt.float16:
        flags |= 1 << int(trt.BuilderFlag.FP16)

    min_prefix = []
    opt_prefix = []
    max_prefix = []

    if max_batch != 0:
        min_prefix = [1]
        opt_prefix = [max(1, max_batch)]
        max_prefix = [max(1, max_batch)]

    min_shape = min_prefix + [1] * len(shape)
    opt_shape = opt_prefix + [8] * len(shape)
    max_shape = max_prefix + [32] * len(shape)

    profile = builder.create_optimization_profile()
    profile.set_shape("INPUT", min_shape, opt_shape, max_shape)
    profile.set_shape_input("SHAPE_INPUT", min_shape, opt_shape, max_shape)
    profile.set_shape("DUMMY_INPUT", min_shape, opt_shape, max_shape)
    profile.set_shape(
        "START",
        min_prefix + unit_shape,
        opt_prefix + unit_shape,
        max_prefix + unit_shape,
    )
    profile.set_shape(
        "END", min_prefix + unit_shape, opt_prefix + unit_shape, max_prefix + unit_shape
    )
    profile.set_shape(
        "READY",
        min_prefix + unit_shape,
        opt_prefix + unit_shape,
        max_prefix + unit_shape,
    )
    profile.set_shape(
        "CORRID",
        min_prefix + unit_shape,
        opt_prefix + unit_shape,
        max_prefix + unit_shape,
    )

    config = builder.create_builder_config()
    config.flags = flags
    config.add_optimization_profile(profile)
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 20)
    try:
        engine_bytes = builder.build_serialized_network(network, config)
    except AttributeError:
        engine = builder.build_engine(network, config)
        engine_bytes = engine.serialize()
        del engine

    model_name = tu.get_dyna_sequence_model_name(
        "plan_nobatch" if max_batch == 0 else "plan", dtype
    )
    model_name = model_name + "_" + np.dtype(shape_tensor_input_dtype).name
    model_version_dir = models_dir + "/" + model_name + "/" + str(model_version)

    try:
        os.makedirs(model_version_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(model_version_dir + "/model.plan", "wb") as f:
        f.write(engine_bytes)


def create_plan_modelfile(models_dir, model_version, max_batch, dtype, shape):
    trt_dtype = np_to_trt_dtype(dtype)
    # Create the model. For now don't implement a proper accumulator
    # just return 0 if not-ready and 'INPUT'+'START'*('END'*'CORRID')
    # otherwise...  the tests know to expect this.
    TRT_LOGGER = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(TRT_LOGGER)
    network = builder.create_network()

    unit_shape = [1] * len(shape)
    if max_batch != 0:
        in0 = network.add_input("INPUT", trt_dtype, [-1] + shape)
        start0 = network.add_input("START", trt_dtype, [-1] + unit_shape)
        end0 = network.add_input("END", trt_dtype, [-1] + unit_shape)
        ready0 = network.add_input("READY", trt_dtype, [-1] + unit_shape)
        corrid0 = network.add_input("CORRID", trt.int32, [-1] + unit_shape)
    else:
        in0 = network.add_input("INPUT", trt_dtype, shape)
        start0 = network.add_input("START", trt_dtype, unit_shape)
        end0 = network.add_input("END", trt_dtype, unit_shape)
        ready0 = network.add_input("READY", trt_dtype, unit_shape)
        corrid0 = network.add_input("CORRID", trt.int32, unit_shape)

    add0 = network.add_elementwise(in0, start0, trt.ElementWiseOperation.SUM)
    mul0 = network.add_elementwise(end0, corrid0, trt.ElementWiseOperation.PROD)
    sum0 = network.add_elementwise(
        add0.get_output(0), mul0.get_output(0), trt.ElementWiseOperation.SUM
    )
    out0 = network.add_elementwise(
        sum0.get_output(0), ready0, trt.ElementWiseOperation.PROD
    )

    out0.get_output(0).name = "OUTPUT"
    network.mark_output(out0.get_output(0))

    min_shape = []
    opt_shape = []
    max_shape = []
    if max_batch != 0:
        min_shape = min_shape + [1]
        opt_shape = opt_shape + [max(1, max_batch)]
        max_shape = max_shape + [max(1, max_batch)]
    for i in shape:
        if i == -1:
            min_shape = min_shape + [1]
            opt_shape = opt_shape + [8]
            max_shape = max_shape + [32]
        else:
            min_shape = min_shape + [i]
            opt_shape = opt_shape + [i]
            max_shape = max_shape + [i]

    profile = builder.create_optimization_profile()
    profile.set_shape("INPUT", min_shape, opt_shape, max_shape)
    if max_batch != 0:
        profile.set_shape(
            "START",
            [1] + unit_shape,
            [max_batch] + unit_shape,
            [max_batch] + unit_shape,
        )
        profile.set_shape(
            "END", [1] + unit_shape, [max_batch] + unit_shape, [max_batch] + unit_shape
        )
        profile.set_shape(
            "READY",
            [1] + unit_shape,
            [max_batch] + unit_shape,
            [max_batch] + unit_shape,
        )
        profile.set_shape(
            "CORRID",
            [1] + unit_shape,
            [max_batch] + unit_shape,
            [max_batch] + unit_shape,
        )
    else:
        profile.set_shape("START", unit_shape, unit_shape, unit_shape)
        profile.set_shape("END", unit_shape, unit_shape, unit_shape)
        profile.set_shape("READY", unit_shape, unit_shape, unit_shape)
        profile.set_shape("CORRID", unit_shape, unit_shape, unit_shape)
    config = builder.create_builder_config()
    config.add_optimization_profile(profile)

    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 20)
    try:
        engine_bytes = builder.build_serialized_network(network, config)
    except AttributeError:
        engine = builder.build_engine(network, config)
        engine_bytes = engine.serialize()
        del engine

    model_name = tu.get_dyna_sequence_model_name(
        "plan_nobatch" if max_batch == 0 else "plan", dtype
    )
    model_version_dir = models_dir + "/" + model_name + "/" + str(model_version)

    try:
        os.makedirs(model_version_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(model_version_dir + "/model.plan", "wb") as f:
        f.write(engine_bytes)


def create_plan_rf_modelfile(models_dir, model_version, max_batch, dtype, shape):
    trt_dtype = np_to_trt_dtype(dtype)
    trt_memory_format = trt.TensorFormat.LINEAR

    # Create the model. For now don't implement a proper accumulator
    # just return 0 if not-ready and 'INPUT'+'START'*('END'*'CORRID')
    # otherwise...  the tests know to expect this.
    TRT_LOGGER = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(TRT_LOGGER)
    network = builder.create_network()

    unit_shape = [1] * len(shape)
    if max_batch != 0:
        in0 = network.add_input("INPUT", trt_dtype, [-1] + shape)
        start0 = network.add_input("START", trt_dtype, [-1] + unit_shape)
        end0 = network.add_input("END", trt_dtype, [-1] + unit_shape)
        ready0 = network.add_input("READY", trt_dtype, [-1] + unit_shape)
        corrid0 = network.add_input("CORRID", trt.int32, [-1] + unit_shape)
    else:
        in0 = network.add_input("INPUT", trt_dtype, shape)
        start0 = network.add_input("START", trt_dtype, unit_shape)
        end0 = network.add_input("END", trt_dtype, unit_shape)
        ready0 = network.add_input("READY", trt_dtype, unit_shape)
        corrid0 = network.add_input("CORRID", trt.int32, unit_shape)

    add0 = network.add_elementwise(in0, start0, trt.ElementWiseOperation.SUM)
    mul0 = network.add_elementwise(end0, corrid0, trt.ElementWiseOperation.PROD)
    sum0 = network.add_elementwise(
        add0.get_output(0), mul0.get_output(0), trt.ElementWiseOperation.SUM
    )
    out0 = network.add_elementwise(
        sum0.get_output(0), ready0, trt.ElementWiseOperation.PROD
    )

    out0.get_output(0).name = "OUTPUT"
    network.mark_output(out0.get_output(0))

    out0.get_output(0).dtype = trt_dtype

    in0.allowed_formats = 1 << int(trt_memory_format)
    start0.allowed_formats = 1 << int(trt_memory_format)
    ready0.allowed_formats = 1 << int(trt_memory_format)
    out0.get_output(0).allowed_formats = 1 << int(trt_memory_format)

    if trt_dtype == trt.int8:
        in0.dynamic_range = (-128.0, 127.0)
        out0.dynamic_range = (-128.0, 127.0)
        start0.dynamic_range = (-128.0, 127.0)
        end0.dynamic_range = (-128.0, 127.0)
        ready0.dynamic_range = (-128.0, 127.0)
        corrid0.dynamic_range = (-128.0, 127.0)

    flags = 1 << int(trt.BuilderFlag.DIRECT_IO)
    flags |= 1 << int(trt.BuilderFlag.PREFER_PRECISION_CONSTRAINTS)
    flags |= 1 << int(trt.BuilderFlag.REJECT_EMPTY_ALGORITHMS)

    if trt_dtype == trt.int8:
        flags |= 1 << int(trt.BuilderFlag.INT8)
    elif trt_dtype == trt.float16:
        flags |= 1 << int(trt.BuilderFlag.FP16)

    min_shape = []
    opt_shape = []
    max_shape = []
    if max_batch != 0:
        min_shape = min_shape + [1]
        opt_shape = opt_shape + [max(1, max_batch)]
        max_shape = max_shape + [max(1, max_batch)]
    for i in shape:
        if i == -1:
            min_shape = min_shape + [1]
            opt_shape = opt_shape + [8]
            max_shape = max_shape + [32]
        else:
            min_shape = min_shape + [i]
            opt_shape = opt_shape + [i]
            max_shape = max_shape + [i]

    profile = builder.create_optimization_profile()
    profile.set_shape("INPUT", min_shape, opt_shape, max_shape)
    if max_batch != 0:
        profile.set_shape(
            "START",
            [1] + unit_shape,
            [max_batch] + unit_shape,
            [max_batch] + unit_shape,
        )
        profile.set_shape(
            "END", [1] + unit_shape, [max_batch] + unit_shape, [max_batch] + unit_shape
        )
        profile.set_shape(
            "READY",
            [1] + unit_shape,
            [max_batch] + unit_shape,
            [max_batch] + unit_shape,
        )
        profile.set_shape(
            "CORRID",
            [1] + unit_shape,
            [max_batch] + unit_shape,
            [max_batch] + unit_shape,
        )
    else:
        profile.set_shape("START", unit_shape, unit_shape, unit_shape)
        profile.set_shape("END", unit_shape, unit_shape, unit_shape)
        profile.set_shape("READY", unit_shape, unit_shape, unit_shape)
        profile.set_shape("CORRID", unit_shape, unit_shape, unit_shape)

    config = builder.create_builder_config()
    config.flags = flags
    config.add_optimization_profile(profile)
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 20)
    try:
        engine_bytes = builder.build_serialized_network(network, config)
    except AttributeError:
        engine = builder.build_engine(network, config)
        engine_bytes = engine.serialize()
        del engine

    model_name = tu.get_dyna_sequence_model_name(
        "plan_nobatch" if max_batch == 0 else "plan", dtype
    )
    model_version_dir = models_dir + "/" + model_name + "/" + str(model_version)

    try:
        os.makedirs(model_version_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(model_version_dir + "/model.plan", "wb") as f:
        f.write(engine_bytes)


def create_plan_models(models_dir, model_version, max_batch, dtype, shape):
    if not tu.validate_for_trt_model(dtype, dtype, dtype, shape, shape, shape):
        return

    if dtype != np.float32:
        create_plan_rf_modelfile(models_dir, model_version, max_batch, dtype, shape)
    else:
        create_plan_modelfile(models_dir, model_version, max_batch, dtype, shape)


def create_plan_modelconfig(
    models_dir, model_version, max_batch, dtype, shape, shape_tensor_input_dtype=None
):
    if not tu.validate_for_trt_model(dtype, dtype, dtype, shape, shape, shape):
        return

    model_name = tu.get_dyna_sequence_model_name(
        "plan_nobatch" if max_batch == 0 else "plan", dtype
    )
    if shape_tensor_input_dtype:
        model_name = model_name + "_" + np.dtype(shape_tensor_input_dtype).name
    config_dir = models_dir + "/" + model_name

    if FLAGS.tensorrt_shape_io:
        shape_tensor_dim = len(shape)
        config = """
name: "{}"
platform: "tensorrt_plan"
max_batch_size: {}
sequence_batching {{
  max_sequence_idle_microseconds: 5000000
  {}
  control_input [
    {{
      name: "START"
      control [
        {{
          kind: CONTROL_SEQUENCE_START
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "END"
      control [
        {{
          kind: CONTROL_SEQUENCE_END
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "READY"
      control [
        {{
          kind: CONTROL_SEQUENCE_READY
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "CORRID"
      control [
        {{
          kind: CONTROL_SEQUENCE_CORRID
          data_type: TYPE_INT32
        }}
      ]
    }}
  ]
}}
input [
  {{
    name: "INPUT"
    data_type: TYPE_INT32
    dims: [ {} ]
  }}
]
input [
  {{
    name: "DUMMY_INPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
input [
  {{
    name: "SHAPE_INPUT"
    data_type: {}
    dims: [ {} ]
    is_shape_tensor: true
  }}
]
output [
  {{
    name: "OUTPUT"
    data_type: TYPE_INT32
    dims: [ {} ]
  }}
]
output [
  {{
    name: "RESIZED_OUTPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
output [
  {{
    name: "SHAPE_OUTPUT"
    data_type: TYPE_INT64
    dims: [ {} ]
    is_shape_tensor: true
  }}
]
instance_group [
  {{
    kind: KIND_GPU
  }}
]
""".format(
            model_name,
            max_batch,
            (
                "oldest { max_candidate_sequences: 6\npreferred_batch_size: [ 4 ]\nmax_queue_delay_microseconds: 0\n}"
                if max_batch > 0
                else ""
            ),
            "int32",
            "int32",
            "int32",
            tu.shape_to_dims_str(shape),
            np_to_model_dtype(dtype),
            tu.shape_to_dims_str(shape),
            np_to_model_dtype(shape_tensor_input_dtype),
            shape_tensor_dim,
            tu.shape_to_dims_str(shape),
            np_to_model_dtype(dtype),
            tu.shape_to_dims_str(shape),
            shape_tensor_dim,
        )

    else:
        config = """
name: "{}"
platform: "tensorrt_plan"
max_batch_size: {}
sequence_batching {{
  max_sequence_idle_microseconds: 5000000
  {}
  control_input [
    {{
      name: "START"
      control [
        {{
          kind: CONTROL_SEQUENCE_START
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "END"
      control [
        {{
          kind: CONTROL_SEQUENCE_END
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "READY"
      control [
        {{
          kind: CONTROL_SEQUENCE_READY
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "CORRID"
      control [
        {{
          kind: CONTROL_SEQUENCE_CORRID
          data_type: TYPE_INT32
        }}
      ]
    }}
  ]
}}
input [
  {{
    name: "INPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
output [
  {{
    name: "OUTPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
instance_group [
  {{
    kind: KIND_GPU
  }}
]
""".format(
            model_name,
            max_batch,
            (
                "oldest { max_candidate_sequences: 6\npreferred_batch_size: [ 4 ]\nmax_queue_delay_microseconds: 0\n}"
                if max_batch > 0
                else ""
            ),
            "int32" if dtype == np.int32 else "fp32",
            "int32" if dtype == np.int32 else "fp32",
            "int32" if dtype == np.int32 else "fp32",
            np_to_model_dtype(dtype),
            tu.shape_to_dims_str(shape),
            np_to_model_dtype(dtype),
            tu.shape_to_dims_str(shape),
        )

    try:
        os.makedirs(config_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(config_dir + "/config.pbtxt", "w") as cfile:
        cfile.write(config)


def create_onnx_modelfile(models_dir, model_version, max_batch, dtype, shape):
    if not tu.validate_for_onnx_model(dtype, dtype, dtype, shape, shape, shape):
        return

    model_name = tu.get_dyna_sequence_model_name(
        "onnx_nobatch" if max_batch == 0 else "onnx", dtype
    )
    model_version_dir = models_dir + "/" + model_name + "/" + str(model_version)

    # Create the model. For now don't implement a proper accumulator
    # just return 0 if not-ready and 'INPUT'+'START'*('END'*'CORRID')
    # otherwise...  the tests know to expect this.
    onnx_dtype = np_to_onnx_dtype(dtype)
    onnx_input_shape, idx = tu.shape_to_onnx_shape(shape, 0)
    onnx_output_shape, idx = tu.shape_to_onnx_shape(shape, idx)

    # If the input is a string then use int32 for operation and just
    # cast to/from string for input and output.
    onnx_control_dtype = onnx_dtype
    if onnx_dtype == onnx.TensorProto.STRING:
        onnx_control_dtype = onnx.TensorProto.INT32

    batch_dim = [] if max_batch == 0 else [None]

    onnx_input = onnx.helper.make_tensor_value_info(
        "INPUT", onnx_dtype, batch_dim + onnx_input_shape
    )
    onnx_start = onnx.helper.make_tensor_value_info(
        "START", onnx_control_dtype, batch_dim + [1]
    )
    onnx_end = onnx.helper.make_tensor_value_info(
        "END", onnx_control_dtype, batch_dim + [1]
    )
    onnx_ready = onnx.helper.make_tensor_value_info(
        "READY", onnx_control_dtype, batch_dim + [1]
    )
    onnx_corrid = onnx.helper.make_tensor_value_info(
        "CORRID", onnx.TensorProto.UINT64, batch_dim + [1]
    )
    onnx_output = onnx.helper.make_tensor_value_info(
        "OUTPUT", onnx_dtype, batch_dim + onnx_output_shape
    )

    internal_input = onnx.helper.make_node("Identity", ["INPUT"], ["_INPUT"])

    # cast int8, int16 input to higher precision int as Onnx Add/Sub operator doesn't support those type
    # Also casting String data type to int32
    if (
        (onnx_dtype == onnx.TensorProto.INT8)
        or (onnx_dtype == onnx.TensorProto.INT16)
        or (onnx_dtype == onnx.TensorProto.STRING)
    ):
        internal_input = onnx.helper.make_node(
            "Cast", ["INPUT"], ["_INPUT"], to=onnx.TensorProto.INT32
        )

    onnx_corrid_cast0 = onnx.helper.make_node(
        "Cast", ["CORRID"], ["onnx_corrid_cast0"], to=onnx_control_dtype
    )
    add0 = onnx.helper.make_node("Add", ["_INPUT", "START"], ["add0"])
    mul0 = onnx.helper.make_node("Mul", ["END", "onnx_corrid_cast0"], ["mul0"])
    sum0 = onnx.helper.make_node("Add", ["add0", "mul0"], ["sum0"])
    res0 = onnx.helper.make_node("Mul", ["READY", "sum0"], ["CAST"])
    cast = onnx.helper.make_node("Cast", ["CAST"], ["OUTPUT"], to=onnx_dtype)

    # Avoid cast from float16 to float16
    # (bug in Onnx Runtime, cast from float16 to float16 will become cast from float16 to float32)
    if onnx_dtype == onnx.TensorProto.FLOAT16:
        cast = onnx.helper.make_node("Identity", ["CAST"], ["OUTPUT"])

    onnx_nodes = [internal_input, onnx_corrid_cast0, add0, mul0, sum0, res0, cast]
    onnx_inputs = [onnx_input, onnx_start, onnx_end, onnx_ready, onnx_corrid]
    onnx_outputs = [onnx_output]

    graph_proto = onnx.helper.make_graph(
        onnx_nodes, model_name, onnx_inputs, onnx_outputs
    )
    if FLAGS.onnx_opset > 0:
        model_opset = onnx.helper.make_operatorsetid("", FLAGS.onnx_opset)
        model_def = onnx.helper.make_model(
            graph_proto, producer_name="triton", opset_imports=[model_opset]
        )
    else:
        model_def = onnx.helper.make_model(graph_proto, producer_name="triton")

    try:
        os.makedirs(model_version_dir)
    except OSError as ex:
        pass  # ignore existing dir

    onnx.save(model_def, model_version_dir + "/model.onnx")


def create_onnx_modelconfig(models_dir, model_version, max_batch, dtype, shape):
    if not tu.validate_for_onnx_model(dtype, dtype, dtype, shape, shape, shape):
        return

    model_name = tu.get_dyna_sequence_model_name(
        "onnx_nobatch" if max_batch == 0 else "onnx", dtype
    )
    config_dir = models_dir + "/" + model_name
    config = """
name: "{}"
platform: "onnxruntime_onnx"
max_batch_size: {}
sequence_batching {{
  max_sequence_idle_microseconds: 5000000
  {}
  control_input [
    {{
      name: "START"
      control [
        {{
          kind: CONTROL_SEQUENCE_START
          {type}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "END"
      control [
        {{
          kind: CONTROL_SEQUENCE_END
          {type}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "READY"
      control [
        {{
          kind: CONTROL_SEQUENCE_READY
          {type}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "CORRID"
      control [
        {{
          kind: CONTROL_SEQUENCE_CORRID
          data_type: TYPE_UINT64
        }}
      ]
    }}
  ]
}}
input [
  {{
    name: "INPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
output [
  {{
    name: "OUTPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
instance_group [
  {{
    kind: KIND_CPU
  }}
]
""".format(
        model_name,
        max_batch,
        (
            "oldest { max_candidate_sequences: 6\npreferred_batch_size: [ 4 ]\nmax_queue_delay_microseconds: 0\n}"
            if max_batch > 0
            else ""
        ),
        np_to_model_dtype(dtype),
        tu.shape_to_dims_str(shape),
        np_to_model_dtype(dtype),
        tu.shape_to_dims_str(shape),
        type="fp32" if dtype == np.float32 else "int32",
    )

    try:
        os.makedirs(config_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(config_dir + "/config.pbtxt", "w") as cfile:
        cfile.write(config)


def create_libtorch_modelfile(models_dir, model_version, max_batch, dtype, shape):
    if not tu.validate_for_libtorch_model(dtype, dtype, dtype, shape, shape, shape):
        return

    torch_dtype = np_to_torch_dtype(dtype)

    model_name = tu.get_dyna_sequence_model_name(
        "libtorch_nobatch" if max_batch == 0 else "libtorch", dtype
    )
    # handle for -1 (when variable) since can't create tensor with shape of [-1]
    shape = [abs(ips) for ips in shape]

    class SequenceNet(nn.Module):
        def __init__(self):
            super(SequenceNet, self).__init__()

        def forward(self, input0, start0, end0, ready0, corrid0):
            tmp = input0 + start0 + (end0 * corrid0)
            return tmp * ready0

    sequenceModel = SequenceNet()
    example_input = torch.zeros(shape, dtype=torch_dtype)
    example_corrid_input = torch.zeros(shape, dtype=torch.long)
    traced = torch.jit.trace(
        sequenceModel,
        (
            example_input,
            example_input,
            example_input,
            example_input,
            example_corrid_input,
        ),
    )

    model_version_dir = models_dir + "/" + model_name + "/" + str(model_version)

    try:
        os.makedirs(model_version_dir)
    except OSError as ex:
        pass  # ignore existing dir

    traced.save(model_version_dir + "/model.pt")


def create_libtorch_modelconfig(models_dir, model_version, max_batch, dtype, shape):
    if not tu.validate_for_libtorch_model(dtype, dtype, dtype, shape, shape, shape):
        return

    model_name = tu.get_dyna_sequence_model_name(
        "libtorch_nobatch" if max_batch == 0 else "libtorch", dtype
    )
    config_dir = models_dir + "/" + model_name
    #  FIX FOR LibTorch
    config = """
name: "{}"
platform: "pytorch_libtorch"
max_batch_size: {}
sequence_batching {{
  max_sequence_idle_microseconds: 5000000
  {}
  control_input [
    {{
      name: "START__1"
      control [
        {{
          kind: CONTROL_SEQUENCE_START
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "END__2"
      control [
        {{
          kind: CONTROL_SEQUENCE_END
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "READY__3"
      control [
        {{
          kind: CONTROL_SEQUENCE_READY
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "CORRID__4"
      control [
        {{
          kind: CONTROL_SEQUENCE_CORRID
          data_type: TYPE_INT32
        }}
      ]
    }}
  ]
}}
input [
  {{
    name: "INPUT__0"
    data_type: {}
    dims: [ {} ]
  }}
]
output [
  {{
    name: "OUTPUT__0"
    data_type: {}
    dims: [ 1 ]
  }}
]
instance_group [
  {{
    kind: KIND_CPU
  }}
]
""".format(
        model_name,
        max_batch,
        (
            "oldest { max_candidate_sequences: 6\npreferred_batch_size: [ 4 ]\nmax_queue_delay_microseconds: 0\n}"
            if max_batch > 0
            else ""
        ),
        "int32" if dtype == np.int32 else "fp32",
        "int32" if dtype == np.int32 else "fp32",
        "int32" if dtype == np.int32 else "fp32",
        np_to_model_dtype(dtype),
        tu.shape_to_dims_str(shape),
        np_to_model_dtype(dtype),
    )

    try:
        os.makedirs(config_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(config_dir + "/config.pbtxt", "w") as cfile:
        cfile.write(config)


def create_openvino_modelfile(models_dir, model_version, max_batch, dtype, shape):
    batch_dim = (
        []
        if max_batch == 0
        else [
            max_batch,
        ]
    )
    if not tu.validate_for_openvino_model(
        dtype, dtype, dtype, batch_dim + shape, batch_dim + shape, batch_dim + shape
    ):
        return

    model_name = tu.get_dyna_sequence_model_name(
        "openvino_nobatch" if max_batch == 0 else "openvino", dtype
    )
    model_version_dir = models_dir + "/" + model_name + "/" + str(model_version)

    in0 = ov.opset1.parameter(shape=batch_dim + shape, dtype=dtype, name="INPUT")
    start = ov.opset1.parameter(shape=batch_dim + shape, dtype=dtype, name="START")
    end = ov.opset1.parameter(shape=batch_dim + shape, dtype=dtype, name="END")
    ready = ov.opset1.parameter(shape=batch_dim + shape, dtype=dtype, name="READY")
    corrid = ov.opset1.parameter(shape=batch_dim + shape, dtype=dtype, name="CORRID")

    tmp1 = ov.opset1.add(in0, start)
    tmp2 = ov.opset1.multiply(end, corrid)
    tmp = ov.opset1.add(tmp1, tmp2)
    op0 = ov.opset1.multiply(tmp, ready, name="OUTPUT")

    model = ov.Model([op0], [in0, start, end, ready, corrid], model_name)
    openvino_save_model(model_version_dir, model)


def create_openvino_modelconfig(models_dir, model_version, max_batch, dtype, shape):
    batch_dim = (
        []
        if max_batch == 0
        else [
            max_batch,
        ]
    )
    if not tu.validate_for_openvino_model(
        dtype, dtype, dtype, batch_dim + shape, batch_dim + shape, batch_dim + shape
    ):
        return

    model_name = tu.get_dyna_sequence_model_name(
        "openvino_nobatch" if max_batch == 0 else "openvino", dtype
    )
    config_dir = models_dir + "/" + model_name
    config = """
name: "{}"
backend: "openvino"
max_batch_size: {}
sequence_batching {{
  max_sequence_idle_microseconds: 5000000
  {}
  control_input [
    {{
      name: "START"
      control [
        {{
          kind: CONTROL_SEQUENCE_START
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "END"
      control [
        {{
          kind: CONTROL_SEQUENCE_END
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "READY"
      control [
        {{
          kind: CONTROL_SEQUENCE_READY
          {}_false_true: [ 0, 1 ]
        }}
      ]
    }},
    {{
      name: "CORRID"
      control [
        {{
          kind: CONTROL_SEQUENCE_CORRID
          data_type: TYPE_INT32
        }}
      ]
    }}
  ]
}}
input [
  {{
    name: "INPUT"
    data_type: {}
    dims: [ {} ]
  }}
]
output [
  {{
    name: "OUTPUT"
    data_type: {}
    dims: [ 1 ]
  }}
]
""".format(
        model_name,
        max_batch,
        (
            "oldest { max_candidate_sequences: 6\npreferred_batch_size: [ 4 ]\nmax_queue_delay_microseconds: 0\n}"
            if max_batch > 0
            else ""
        ),
        "int32" if dtype == np.int32 else "fp32",
        "int32" if dtype == np.int32 else "fp32",
        "int32" if dtype == np.int32 else "fp32",
        np_to_model_dtype(dtype),
        tu.shape_to_dims_str(shape),
        np_to_model_dtype(dtype),
    )

    try:
        os.makedirs(config_dir)
    except OSError as ex:
        pass  # ignore existing dir

    with open(config_dir + "/config.pbtxt", "w") as cfile:
        cfile.write(config)


def create_shape_tensor_models(
    models_dir, dtype, shape, shape_tensor_input_dtype, no_batch=True
):
    model_version = 1

    create_plan_modelconfig(
        models_dir, model_version, 8, dtype, shape, shape_tensor_input_dtype
    )
    create_plan_shape_tensor_modelfile(
        models_dir, model_version, 8, dtype, shape, shape_tensor_input_dtype
    )
    if no_batch:
        create_plan_modelconfig(
            models_dir, model_version, 0, dtype, shape, shape_tensor_input_dtype
        )
        create_plan_shape_tensor_modelfile(
            models_dir, model_version, 0, dtype, shape, shape_tensor_input_dtype
        )


def create_models(models_dir, dtype, shape, no_batch=True):
    model_version = 1

    if FLAGS.tensorrt:
        suffix = []
        if dtype == np.int8:
            suffix = [1, 1]

        create_plan_modelconfig(models_dir, model_version, 8, dtype, shape + suffix)
        create_plan_models(models_dir, model_version, 8, dtype, shape + suffix)
        if no_batch:
            create_plan_modelconfig(models_dir, model_version, 0, dtype, shape + suffix)
            create_plan_models(models_dir, model_version, 0, dtype, shape + suffix)

    if FLAGS.onnx:
        create_onnx_modelconfig(models_dir, model_version, 8, dtype, shape)
        create_onnx_modelfile(models_dir, model_version, 8, dtype, shape)
        if no_batch:
            create_onnx_modelconfig(models_dir, model_version, 0, dtype, shape)
            create_onnx_modelfile(models_dir, model_version, 0, dtype, shape)

    if FLAGS.libtorch:
        create_libtorch_modelconfig(models_dir, model_version, 8, dtype, shape)
        create_libtorch_modelfile(models_dir, model_version, 8, dtype, shape)
        if no_batch:
            create_libtorch_modelconfig(models_dir, model_version, 0, dtype, shape)
            create_libtorch_modelfile(models_dir, model_version, 0, dtype, shape)

    if FLAGS.openvino:
        create_openvino_modelconfig(models_dir, model_version, 8, dtype, shape)
        create_openvino_modelfile(models_dir, model_version, 8, dtype, shape)
        if no_batch:
            create_openvino_modelconfig(models_dir, model_version, 0, dtype, shape)
            create_openvino_modelfile(models_dir, model_version, 0, dtype, shape)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models_dir", type=str, required=True, help="Top-level model directory"
    )
    parser.add_argument(
        "--tensorrt",
        required=False,
        action="store_true",
        help="Generate TensorRT PLAN models",
    )
    parser.add_argument(
        "--tensorrt-shape-io",
        required=False,
        action="store_true",
        help="Generate TensorRT PLAN models w/ shape tensor i/o",
    )
    parser.add_argument(
        "--onnx", required=False, action="store_true", help="Generate Onnx models"
    )
    parser.add_argument(
        "--onnx_opset",
        type=int,
        required=False,
        default=0,
        help="Opset used for Onnx models. Default is to use ONNXRT default",
    )
    parser.add_argument(
        "--libtorch",
        required=False,
        action="store_true",
        help="Generate Pytorch LibTorch models",
    )
    parser.add_argument(
        "--openvino",
        required=False,
        action="store_true",
        help="Generate OpenVino models",
    )
    parser.add_argument(
        "--variable",
        required=False,
        action="store_true",
        help="Used variable-shape tensors for input/output",
    )
    FLAGS, unparsed = parser.parse_known_args()

    if FLAGS.tensorrt or FLAGS.tensorrt_shape_io:
        import tensorrt as trt
    if FLAGS.onnx:
        import onnx
    if FLAGS.libtorch:
        import torch
        from torch import nn
    if FLAGS.openvino:
        import openvino.runtime as ov

    import test_util as tu

    if FLAGS.tensorrt_shape_io:
        create_shape_tensor_models(
            FLAGS.models_dir,
            np.float32,
            [
                -1,
            ],
            np.int32,
        )
        create_shape_tensor_models(
            FLAGS.models_dir,
            np.float32,
            [
                -1,
            ],
            np.int64,
        )
    else:
        # Tests with models that accept fixed-shape input/output tensors
        if not FLAGS.variable:
            create_models(
                FLAGS.models_dir,
                np.int32,
                [
                    1,
                ],
            )

        # Tests with models that accept variable-shape input/output tensors
        if FLAGS.variable:
            create_models(
                FLAGS.models_dir,
                np.int32,
                [
                    -1,
                ],
                False,
            )
