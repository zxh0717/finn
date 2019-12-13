import os

import numpy as np

from finn.backend.fpgadataflow.utils import numpy_to_hls_code
from finn.core.datatype import DataType
from finn.core.utils import interleave_matrix_outer_dim_from_partitions
from finn.custom_op.fpgadataflow import HLSCustomOp


class ConvLayer_Batch(HLSCustomOp):
    def __init__(self, onnx_node):
        super().__init__(onnx_node)

    def get_nodeattr_types(self):
        my_attrs = {
            "ConvKernelDim": ("i", True, 0),
            "IFMChannels": ("i", True, 0),
            "IFMDim": ("i", True, 0),
            "OFMChannels": ("i", True, 0),
            "OFMDim": ("i", True, 0),
            "SIMD": ("i", True, 0),
            "PE": ("i", True, 0),
            "resType": ("s", True, ""),
            "ActVal": ("i", False, 0),
            # FINN DataTypes for inputs, weights, outputs
            "inputDataType": ("s", True, ""),
            "weightDataType": ("s", True, ""),
            "outputDataType": ("s", True, ""),
            "binaryXnorMode": ("i", False, 0),
            "noActivation": ("i", False, 0),
        }
        my_attrs.update(super().get_nodeattr_types())
        return my_attrs

    def calc_mw(self):
        k = self.get_nodeattr("ConvKernelDim")
        ifm_ch = self.get_nodeattr("IFMChannels")
        return k * k * ifm_ch

    def calc_mh(self):
        ofm_ch = self.get_nodeattr("OFMChannels")
        return ofm_ch

    def calc_wmem(self):
        mw = self.calc_mw()
        mh = self.calc_mh()
        pe = self.get_nodeattr("PE")
        simd = self.get_nodeattr("SIMD")
        assert mh % pe == 0
        assert mw % simd == 0
        wmem = mw * mh // (pe * simd)
        return wmem

    def calc_tmem(self):
        if self.get_nodeattr("noActivation") == 1:
            return 0
        else:
            mh = self.calc_mh()
            pe = self.get_nodeattr("PE")
            return mh // pe

    def make_shape_compatible_op(self):
        pass

    def infer_node_datatype(self, model):
        pass

    def verify_node(self):
        pass

    def get_input_datatype(self):
        return DataType[self.get_nodeattr("inputDataType")]

    def get_weight_datatype(self):
        return DataType[self.get_nodeattr("weightDataType")]

    def get_output_datatype(self):
        return DataType[self.get_nodeattr("outputDataType")]

    def get_instream_width(self):
        i_bits = self.get_input_datatype().bitwidth()
        return i_bits * self.get_nodeattr("SIMD")

    def get_outstream_width(self):
        o_bits = self.get_output_datatype().bitwidth()
        return o_bits * self.get_nodeattr("PE")

    def get_template_param_values(self):
        ret = dict()
        inp_hls_str = self.get_input_datatype().get_hls_datatype_str()
        out_hls_str = self.get_output_datatype().get_hls_datatype_str()
        inp_is_binary = self.get_input_datatype() == DataType.BINARY
        out_is_binary = self.get_output_datatype() == DataType.BINARY
        wt_is_binary = self.get_weight_datatype() == DataType.BINARY
        bin_xnor_mode = self.get_nodeattr("binaryXnorMode") == 1
        if (inp_is_binary or wt_is_binary) and (not bin_xnor_mode):
            raise Exception("True binary (non-bipolar) inputs not yet supported")
        inp_is_bipolar = self.get_input_datatype() == DataType.BIPOLAR
        out_is_bipolar = self.get_output_datatype() == DataType.BIPOLAR
        wt_is_bipolar = self.get_weight_datatype() == DataType.BIPOLAR
        # reinterpret inp/wt as bipolar if bin_xnor_mode is iset
        inp_is_bipolar = inp_is_bipolar or (inp_is_binary and bin_xnor_mode)
        wt_is_bipolar = wt_is_bipolar or (wt_is_binary and bin_xnor_mode)
        # fill in TSrcI and TWeightI
        # TODO check these with Giulio
        # TODO handle non-bipolar binary inputs
        if inp_is_bipolar and wt_is_bipolar:
            ret["TSrcI"] = "Recast<XnorMul>"
            ret["TWeightI"] = "Identity"
        elif (not inp_is_bipolar) and wt_is_bipolar:
            ret["TSrcI"] = "Slice<%s>" % inp_hls_str
            ret["TWeightI"] = "Recast<Binary>"
        elif inp_is_bipolar and (not wt_is_bipolar):
            ret["TSrcI"] = "Recast<Binary>"
            ret["TWeightI"] = "Identity"
        elif (not inp_is_bipolar) and (not wt_is_bipolar):
            ret["TSrcI"] = "Slice<%s>" % inp_hls_str
            ret["TWeightI"] = "Identity"
        # fill in TDstI
        if out_is_bipolar or out_is_binary:
            ret["TDstI"] = "Identity"
        else:
            ret["TDstI"] = "Slice<%s>" % out_hls_str
        return ret

    def get_hls_compatible_weight_tensor(self, orig_weight_matrix):
        """Convert the original numpy weight matrix orig_weight_matrix into
        a form suitable for passing to the hlslib call:
        -ensure MH % PE == 0 and MW % SIMD == 0
        -for bipolar {-1,+1} weights, convert to binary {0, 1}
        -interleave rows between PEs
        -reshape into (1, PE, WMEM, SIMD) and return"""

        mw = self.calc_mw()
        mh = self.calc_mh()
        pe = self.get_nodeattr("PE")
        simd = self.get_nodeattr("SIMD")
        wmem = self.calc_wmem()
        assert orig_weight_matrix.shape == (mw, mh)
        assert mw % simd == 0
        assert mh % pe == 0
        # start by transposing the original weight matrix, since ONNX and
        # finn-hlslib use different assumptions
        # ONNX uses (in_features, out_features) and matmul(x, W)
        # finn-hlslib uses (out_features, in_features) and matmul(W, x)
        ret = orig_weight_matrix.T
        if self.get_weight_datatype() == DataType.BIPOLAR:
            # convert bipolar to binary
            ret = (ret + 1) / 2
        # interleave rows between PEs and reshape
        # distribute rows between PEs
        ret = interleave_matrix_outer_dim_from_partitions(ret, pe)
        # create SIMD as innermost dimension and add a dummy outer dim
        ret = ret.reshape(1, pe, wmem, simd)
        return ret

    def get_hls_compatible_threshold_tensor(self, orig_thres_matrix):
        """Convert the original numpy weight matrix orig_weight_matrix into
        a form suitable for passing to the hlslib call:
        * ensure MH % PE == 0
        * for bipolar weights&inputs, ensure thresholds are positive
        * interleave rows between PEs
        * reshape into (PE, TMEM, n_thres_steps) and return
        """
        mh = self.calc_mh()
        pe = self.get_nodeattr("PE")
        tmem = mh // pe
        assert mh % pe == 0
        assert orig_thres_matrix.ndim == 2
        n_thres_steps = orig_thres_matrix.shape[1]
        inp_is_bipolar = self.get_input_datatype() == DataType.BIPOLAR
        wt_is_bipolar = self.get_weight_datatype() == DataType.BIPOLAR
        # reinterpret inp/wt as bipolar if bin_xnor_mode is iset
        inp_is_binary = self.get_input_datatype() == DataType.BINARY
        wt_is_binary = self.get_weight_datatype() == DataType.BINARY
        bin_xnor_mode = self.get_nodeattr("binaryXnorMode") == 1
        inp_is_bipolar = inp_is_bipolar or (inp_is_binary and bin_xnor_mode)
        wt_is_bipolar = wt_is_bipolar or (wt_is_binary and bin_xnor_mode)
        if inp_is_bipolar and wt_is_bipolar:
            # ensure all thresholds are nonnegative
            assert (orig_thres_matrix >= 0).all()
            # ensure all thresholds are integer
            assert (orig_thres_matrix.astype(np.int32) == orig_thres_matrix).all()
        ret = orig_thres_matrix
        # ensure channels = mh , duplicating if necessary
        if ret.shape[0] == 1:
            ret = np.tile(ret, (mh, 1))
        assert ret.shape[0] == mh
        # distribute rows between PEs
        ret = interleave_matrix_outer_dim_from_partitions(ret, pe)
        assert ret.shape[0] == pe
        assert ret.shape[1] == tmem
        assert ret.shape[2] == n_thres_steps
        return ret.reshape(1, pe, tmem, n_thres_steps)

    def generate_params(self, model):
        # weights
        weights = model.get_initializer(self.onnx_node.input[1])
        # convert weights into hlslib-compatible format
        weight_tensor = self.get_hls_compatible_weight_tensor(weights)
        export_wdt = self.get_weight_datatype()
        # we have converted bipolar weights to binary for export,
        # so use it as such for weight generation
        if self.get_weight_datatype() == DataType.BIPOLAR:
            export_wdt = DataType.BINARY
        weight_hls_code = numpy_to_hls_code(
            weight_tensor, export_wdt, "weights", True, True
        )
        # write weights into params.h
        code_gen_dir = self.get_nodeattr("code_gen_dir")
        f_weights = open("{}/params.h".format(code_gen_dir), "w")

        if export_wdt.bitwidth() != 1:
            f_weights.write(
                "static FixedPointWeights<{},{},{},{}> weights = ".format(
                    self.get_nodeattr("SIMD"),
                    export_wdt.get_hls_datatype_str(),
                    self.get_nodeattr("PE"),
                    self.calc_wmem(),
                )
            )
        else:
            f_weights.write(
                "static BinaryWeights<{},{},{}> weights = ".format(
                    self.get_nodeattr("SIMD"), self.get_nodeattr("PE"), self.calc_wmem()
                )
            )
        f_weights.write(weight_hls_code)
        f_weights.close()
        # thresholds
        if len(self.onnx_node.input) > 2:
            thresholds = model.get_initializer(self.onnx_node.input[2])
            if thresholds is not None:
                threshold_tensor = self.get_hls_compatible_threshold_tensor(thresholds)
                tdt = DataType.INT32
                # use UINT32 threshold export for bipolar times bipolar
                inp_is_bipolar = self.get_input_datatype() == DataType.BIPOLAR
                wt_is_bipolar = self.get_weight_datatype() == DataType.BIPOLAR
                # reinterpret inp/wt as bipolar if bin_xnor_mode is iset
                inp_is_binary = self.get_input_datatype() == DataType.BINARY
                wt_is_binary = self.get_weight_datatype() == DataType.BINARY
                bin_xnor_mode = self.get_nodeattr("binaryXnorMode") == 1
                inp_is_bipolar = inp_is_bipolar or (inp_is_binary and bin_xnor_mode)
                wt_is_bipolar = wt_is_bipolar or (wt_is_binary and bin_xnor_mode)
                if inp_is_bipolar and wt_is_bipolar:
                    tdt = DataType.UINT32
                thresholds_hls_code = numpy_to_hls_code(
                    threshold_tensor, tdt, "thresholds", False, True
                )
                # write thresholds into thresh.h
                code_gen_dir = self.get_nodeattr("code_gen_dir")
                f_thresh = open("{}/thresh.h".format(code_gen_dir), "w")
                tdt_hls = tdt.get_hls_datatype_str()
                # use binary to export bipolar activations
                export_odt = self.get_output_datatype()
                if self.get_output_datatype() == DataType.BIPOLAR:
                    export_odt = DataType.BINARY
                odt_hls = export_odt.get_hls_datatype_str()
                f_thresh.write(
                    "static ThresholdsActivation<{},{},{},{},{},{},{}> threshs \
                     = ".format(
                        self.calc_tmem(),
                        self.get_nodeattr("PE"),
                        threshold_tensor.shape[-1],
                        tdt_hls,
                        odt_hls,
                        self.get_nodeattr("ActVal"),
                        "std::less_equal<%s>" % tdt_hls,
                    )
                )
                f_thresh.write(thresholds_hls_code)
                f_thresh.close()

    def execute_node(self, context, graph):
        node = self.onnx_node
        ifm_dim = self.get_nodeattr("IFMDIM")
        ifm_ch = self.get_nodeattr("IFMChannels")
        # ofm_dim = self.get_nodeattr("OFMDIM")
        ofm_ch = self.get_nodeattr("OFMChannels")
        simd = self.get_nodeattr("SIMD")
        pe = self.get_nodeattr("PE")
        sf = ifm_dim // simd
        nf = ofm_ch // pe

        # TODO ensure codegen dir exists
        code_gen_dir = self.get_nodeattr("code_gen_dir")
        # create a npy file fore each input of the node (in_ind is input index)
        in_ind = 0
        for inputs in node.input:
            # it is assumed that the first input of the node is the data input
            # the second input are the weights
            # the third input are the thresholds
            if in_ind == 0:
                assert str(context[inputs].dtype) == "float32"
                expected_inp_shape = (ifm_ch, sf, simd)
                reshaped_input = context[inputs].reshape(expected_inp_shape)
                # flip SIMD (innermost) dimension of input tensor, there's some reversal
                # going on somewhere with a mistmatch between npy and hls...
                reshaped_input = np.flip(reshaped_input, -1)
                if self.get_input_datatype() == DataType.BIPOLAR:
                    # store bipolar activations as binary
                    reshaped_input = (reshaped_input + 1) / 2
                np.save(
                    os.path.join(code_gen_dir, "input_{}.npy".format(in_ind)),
                    reshaped_input,
                )
            elif in_ind > 2:
                raise Exception("Unexpected input found for StreamingFCLayer")
            in_ind += 1
        # execute the precompiled model
        super().exec_precompiled_singlenode_model()
        # load output npy file
        super().npy_to_dynamic_output(context)
        # reinterpret binary output as bipolar where needed
        if self.get_output_datatype() == DataType.BIPOLAR:
            out = context[node.output[0]]
            out = 2 * out - 1
            context[node.output[0]] = out
        assert context[node.output[0]].shape == (ofm_ch, nf, pe)
        # reshape output to have expected shape
        context[node.output[0]] = context[node.output[0]].reshape(ofm_ch, ofm_ch)

    def global_includes(self):
        self.code_gen_dict["$GLOBALS$"] = ['#include "weights.hpp"']
        self.code_gen_dict["$GLOBALS$"] += ['#include "activations.hpp"']
        self.code_gen_dict["$GLOBALS$"] += ['#include "params.h"']
        self.code_gen_dict["$GLOBALS$"] += ['#include "thresh.h"']
        self.code_gen_dict["$GLOBALS$"] += ['#include "mvau.hpp"']
        self.code_gen_dict["$GLOBALS$"] += ['#include "interpret.hpp"']

    def defines(self):
        numReps = 1
        self.code_gen_dict["$DEFINES$"] = [
            """#define ConvKernelDim1 {}\n #define IFMChannels1 {}
            #define IFMDim1 {}\n #define OFMChannels1 {}\n #define OFMDim1 {}
            #define SIMD1 \n #define PE1 {}\n #define WMEM1 {}\n #define TMEM1 {}
            #define numReps {}""".format(
                self.get_nodeattr("ConvKernelDim"),
                self.get_nodeattr("IFMChannels"),
                self.get_nodeattr("IFMDim"),
                self.get_nodeattr("OFMChannels"),
                self.get_nodeattr("OFMDim"),
                self.get_nodeattr("SIMD"),
                self.get_nodeattr("PE"),
                self.calc_wmem(),
                self.calc_tmem(),
                numReps,
            )
        ]

    def read_npy_data(self):
        code_gen_dir = self.get_nodeattr("code_gen_dir")
        dtype = self.get_input_datatype()
        if dtype == DataType.BIPOLAR:
            # use binary for bipolar storage
            dtype = DataType.BINARY
        elem_bits = dtype.bitwidth()
        packed_bits = self.get_instream_width()
        packed_hls_type = "ap_uint<%d>" % packed_bits
        elem_hls_type = dtype.get_hls_datatype_str()
        npy_type = "float"
        npy_in = "%s/input_0.npy" % code_gen_dir
        self.code_gen_dict["$READNPYDATA$"] = []
        self.code_gen_dict["$READNPYDATA$"].append(
            'npy2apintstream<%s, %s, %d, %s>("%s", in0);'
            % (packed_hls_type, elem_hls_type, elem_bits, npy_type, npy_in)
        )

    def strm_decl(self):
        self.code_gen_dict["$STREAMDECLARATIONS$"] = []
        self.code_gen_dict["$STREAMDECLARATIONS$"].append(
            'hls::stream<ap_uint<{}>> in0 ("in0");'.format(self.get_instream_width())
        )
        self.code_gen_dict["$STREAMDECLARATIONS$"].append(
            'hls::stream<ap_uint<{}>> out ("out");'.format(self.get_outstream_width())
        )

    def docompute(self):
        node = self.onnx_node
        tmpl_args = self.get_template_param_values()
        if self.calc_tmem() == 0:
            odtype_hls_str = self.get_output_datatype().get_hls_datatype_str()
            threshs = "PassThroughActivation<%s>()" % odtype_hls_str
        else:
            threshs = "threshs"

        self.code_gen_dict["$DOCOMPUTE$"] = [
            """{}<ConvKernelDim1, IFMChannels1, IFMDim1, OFMChannels1,
            OFMDim1, SIMD1, PE1, {}, {}, {}>
            (in0, out, weights, {}, numReps, {});""".format(
                node.op_type,
                tmpl_args["TSrcI"],
                tmpl_args["TDstI"],
                tmpl_args["TWeightI"],
                threshs,
                self.get_nodeattr("resType"),
            )
        ]

    def dataoutstrm(self):
        code_gen_dir = self.get_nodeattr("code_gen_dir")
        dtype = self.get_output_datatype()
        if dtype == DataType.BIPOLAR:
            # use binary for bipolar storage
            dtype = DataType.BINARY
        elem_bits = dtype.bitwidth()
        packed_bits = self.get_outstream_width()
        packed_hls_type = "ap_uint<%d>" % packed_bits
        elem_hls_type = dtype.get_hls_datatype_str()
        npy_type = "float"
        npy_out = "%s/output.npy" % code_gen_dir
        nf = int(self.get_nodeattr("OFMChannels") / self.get_nodeattr("PE"))
        shape = (1, nf, self.get_nodeattr("PE"))
        shape_cpp_str = str(shape).replace("(", "{").replace(")", "}")

        self.code_gen_dict["$DATAOUTSTREAM$"] = [
            'apintstream2npy<%s, %s, %d, %s>(out, %s, "%s");'
            % (
                packed_hls_type,
                elem_hls_type,
                elem_bits,
                npy_type,
                shape_cpp_str,
                npy_out,
            )
        ]

    def save_as_npy(self):
        self.code_gen_dict["$SAVEASCNPY$"] = []