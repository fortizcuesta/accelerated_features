import types

import argparse
import torch
import torch.nn.functional as F
import onnx
import onnxsim

from modules.xfeat import XFeat


class CustomInstanceNorm(torch.nn.Module):
    def __init__(self, epsilon=1e-5):
        super(CustomInstanceNorm, self).__init__()
        self.epsilon = epsilon

    def forward(self, x):
        mean = x.mean(dim=(2, 3), keepdim=True)
        std = x.std(dim=(2, 3), unbiased=False, keepdim=True)
        return (x - mean) / (std + self.epsilon)


def preprocess_tensor(self, x):
    return x, 1.0, 1.0 # Assuming the width and height are multiples of 32, bypass preprocessing.

def parse_args():
    parser = argparse.ArgumentParser(description="Export XFeat/Matching model to ONNX.")
    parser.add_argument(
        "--xfeat_only",
        action="store_true",
        help="Export only the XFeat model.",
    )
    parser.add_argument(
        "--split_instance_norm",
        action="store_true",
        help="Whether to split InstanceNorm2d into '(x - mean) / (std + epsilon)', due to some inference libraries not supporting InstanceNorm, such as OpenVINO.",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=640,
        help="Input image height.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=640,
        help="Input image width.",
    )
    parser.add_argument(
        "--top_k",
        type=int,
        default=4800,
        help="Keep best k features.",
    )
    parser.add_argument(
        "--dynamic",
        action="store_true",
        help="Enable dynamic axes.",
    )
    parser.add_argument(
        "--export_path",
        type=str,
        default="./model.onnx",
        help="Path to export ONNX model.",
    )
    parser.add_argument(
        "--opset",
        type=int,
        default=11,
        help="ONNX opset version.",
    )

    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if args.dynamic:
        args.height = 640
        args.width = 640
    else:
        assert args.height % 32 == 0 and args.width % 32 == 0, "Height and width must be multiples of 32."

    if args.top_k > 4800:
        print("Warning: The current maximum supported value for TopK in TensorRT is 3840, which coincidentally equals 4800 * 0.8. Please ignore this warning if TensorRT will not be used in the future.")

    batch_size = 2
    x1 = torch.randn(batch_size, 3, args.height, args.width, dtype=torch.float32, device='cpu')
    x2 = torch.randn(batch_size, 3, args.height, args.width, dtype=torch.float32, device='cpu')

    xfeat = XFeat()
    xfeat.top_k = args.top_k

    if args.split_instance_norm:
        xfeat.net.norm = CustomInstanceNorm()

    xfeat = xfeat.cpu().eval()
    xfeat.dev = "cpu"
    xfeat.forward = xfeat.match_xfeat_star

    if not args.dynamic:
        # Bypass preprocess_tensor
        xfeat.preprocess_tensor = types.MethodType(preprocess_tensor, xfeat)

    if args.xfeat_only:
        dynamic_axes = {"images": {0: "batch", 2: "height", 3: "width"}}
        torch.onnx.export(
            xfeat.net,
            (x1),
            args.export_path,
            verbose=False,
            opset_version=args.opset,
            do_constant_folding=True,
            input_names=["images"],
            output_names=["feats", "keypoints", "heatmaps"],
            dynamic_axes=dynamic_axes if args.dynamic else None,
        )
    else:
        dynamic_axes = {"images0": {0: "batch", 2: "height", 3: "width"}, "images1": {0: "batch", 2: "height", 3: "width"}}
        torch.onnx.export(
            xfeat,
            (x1, x2),
            args.export_path,
            verbose=False,
            opset_version=args.opset,
            do_constant_folding=True,
            input_names=["images0", "images1"],
            output_names=["matches", "batch_indexes"],
            dynamic_axes=dynamic_axes if args.dynamic else None,
        )

    model_onnx = onnx.load(args.export_path)  # load onnx model
    onnx.checker.check_model(model_onnx)  # check onnx model

    model_onnx, check = onnxsim.simplify(model_onnx)
    assert check, "assert check failed"
    onnx.save(model_onnx, args.export_path)

    print(f"Model exported to {args.export_path}")
