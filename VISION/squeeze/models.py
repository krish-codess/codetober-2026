"""Teacher, student, structured pruning, distillation training, and ONNX export (PyTorch).

Every network takes float32 NCHW pixels in 0..255 and returns 10 logits. Normalisation is inside
the exported graph, so a device cannot get the preprocessing wrong.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import URLError

import numpy as np
import torch
import torch.nn.functional as F
import torchvision
from torch import nn
from torchvision.models.resnet import Bottleneck

from .data import CLASSES

logger = logging.getLogger("squeeze.models")
IMAGENET_IDX = [c for _, _, c in CLASSES]


class Net(nn.Module):
    def __init__(self, body: nn.Module) -> None:
        super().__init__()
        self.body = body
        self.mean: torch.Tensor
        self.std: torch.Tensor
        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1) * 255)
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1) * 255)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out: torch.Tensor = self.body((x - self.mean) / self.std)
        return out


def _head(linear: nn.Linear, pretrained: bool) -> nn.Linear:
    """The 10 Imagenette rows of an ImageNet classifier: a real 10-class head with no training."""
    new = nn.Linear(linear.in_features, len(CLASSES))
    if pretrained:
        with torch.no_grad():
            new.weight.copy_(linear.weight[IMAGENET_IDX])
            new.bias.copy_(linear.bias[IMAGENET_IDX])
    return new


def _weights(build: Callable[..., Any], weights: str | None, tries: int = 5) -> Any:
    """Pretrained weights come over the network on first use. Retry with capped exponential
    backoff; after that, fail with what to do instead of a urllib traceback."""
    for attempt in range(tries):
        try:
            return build(weights=weights)
        except (URLError, OSError) as exc:
            if weights is None or attempt == tries - 1:
                raise RuntimeError(
                    f"could not download {build.__name__} weights after {tries} attempts ({exc}); "
                    "check the network, or copy the .pth into the torch hub cache and re-run"
                ) from exc
            delay = min(2.0**attempt, 30.0)
            logger.warning("weight download failed (%s); retry %d in %.0fs", exc, attempt + 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def teacher(pretrained: bool = True) -> Net:
    body = _weights(torchvision.models.resnet50, "IMAGENET1K_V2" if pretrained else None)
    body.fc = _head(body.fc, pretrained)
    return Net(body).eval()


def student(pretrained: bool = True) -> Net:
    body = _weights(torchvision.models.mobilenet_v3_small, "IMAGENET1K_V1" if pretrained else None)
    body.classifier[3] = _head(body.classifier[3], pretrained)
    return Net(body).eval()


def n_params(net: nn.Module) -> int:
    return sum(p.numel() for p in net.parameters())


# --- structured pruning -----------------------------------------------------------------------


def _conv(conv: nn.Conv2d, out_idx: torch.Tensor | None, in_idx: torch.Tensor | None) -> nn.Conv2d:
    w = conv.weight.data
    if out_idx is not None:
        w = w[out_idx]
    if in_idx is not None:
        w = w[:, in_idx]
    k: Any = (conv.kernel_size, conv.stride, conv.padding, conv.dilation)
    new = nn.Conv2d(w.shape[1], w.shape[0], k[0], k[1], k[2], k[3], bias=False)
    new.weight.data = w.clone()
    return new


def _bn(bn: nn.BatchNorm2d, idx: torch.Tensor) -> nn.BatchNorm2d:
    new = nn.BatchNorm2d(len(idx))
    for name in ("weight", "bias", "running_mean", "running_var"):
        getattr(new, name).data = getattr(bn, name).data[idx].clone()
    return new


def prune_bottlenecks(net: Net, ratio: float) -> Net:
    """Remove `ratio` of the inner channels of every ResNet bottleneck, lowest L1 norm first.

    Only the two inner widths of a block are touched (conv1->conv2 and conv2->conv3), so the
    residual stream keeps its shape and no other block has to change. The channels are physically
    removed: the result is a smaller dense network that any runtime runs faster, not a sparse one.
    """
    for block in net.modules():
        if not isinstance(block, Bottleneck):
            continue
        for first, norm, second in (("conv1", "bn1", "conv2"), ("conv2", "bn2", "conv3")):
            conv: nn.Conv2d = getattr(block, first)
            keep_n = max(8, round(conv.out_channels * (1 - ratio)))
            score = conv.weight.data.abs().sum(dim=(1, 2, 3)) * getattr(block, norm).weight.data.abs()
            keep = score.topk(keep_n).indices.sort().values
            setattr(block, first, _conv(conv, keep, None))
            setattr(block, norm, _bn(getattr(block, norm), keep))
            setattr(block, second, _conv(getattr(block, second), None, keep))
    return net


# --- training and inference -------------------------------------------------------------------


def _batch(x_u8: np.ndarray, idx: np.ndarray) -> torch.Tensor:
    return torch.from_numpy(np.ascontiguousarray(x_u8[np.sort(idx)])).permute(0, 3, 1, 2).float()


@torch.no_grad()
def predict(net: nn.Module, x_u8: np.ndarray, idx: np.ndarray, batch: int = 50) -> np.ndarray:
    """Logits for images x_u8[idx], in the order of sorted(idx)."""
    net.eval()
    idx = np.sort(idx)
    return np.concatenate([net(_batch(x_u8, idx[i : i + batch])).numpy() for i in range(0, len(idx), batch)])


def train(
    net: nn.Module,
    x_u8: np.ndarray,
    labels: np.ndarray,
    idx: np.ndarray,
    *,
    teacher_logits: np.ndarray | None,
    epochs: int,
    lr: float,
    batch: int = 32,
    temperature: float = 4.0,
    alpha: float = 0.7,
    seed: int = 0,
    progress: Callable[[int, int, float], None] | None = None,
) -> list[float]:
    """Fine-tune on x_u8[idx]. With `teacher_logits` (aligned to sorted(idx)) the loss is
    alpha * T^2 * KL(teacher || student at temperature T) + (1 - alpha) * cross-entropy;
    without, plain cross-entropy. Returns the mean loss of each epoch."""
    torch.manual_seed(seed)
    idx = np.sort(idx)
    rng = np.random.default_rng(seed)
    opt = torch.optim.SGD(net.parameters(), lr=lr, momentum=0.9, nesterov=True, weight_decay=1e-4)
    steps = epochs * ((len(idx) + batch - 1) // batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=steps, pct_start=0.15)
    y_all = torch.from_numpy(labels)
    t_all = None if teacher_logits is None else torch.from_numpy(teacher_logits)
    history: list[float] = []
    step = 0
    for _ in range(epochs):
        net.train()
        order = rng.permutation(len(idx))
        total = 0.0
        for i in range(0, len(order) - 1, batch):  # a trailing batch of 1 would break BatchNorm
            pos = np.sort(order[i : i + batch])
            if len(pos) < 2:
                continue
            logits = net(_batch(x_u8, idx[pos]))
            loss = F.cross_entropy(logits, y_all[idx[pos]])
            if t_all is not None:
                soft = F.kl_div(
                    F.log_softmax(logits / temperature, 1),
                    F.softmax(t_all[pos] / temperature, 1),
                    reduction="batchmean",
                )
                loss = alpha * temperature**2 * soft + (1 - alpha) * loss
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            total += float(loss) * len(pos)
            step += 1
            if progress and step % 20 == 0:
                progress(step, steps, float(loss))
        history.append(total / len(idx))
    net.eval()
    return history


def export(net: nn.Module, path: Path, size: int) -> None:
    """ONNX with a dynamic batch axis. Input `input` float32 Nx3xSxS in 0..255, output `logits`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        net.eval(),
        (torch.zeros(1, 3, size, size),),
        str(path),
        input_names=["input"],
        output_names=["logits"],
        dynamic_axes={"input": {0: "n"}, "logits": {0: "n"}},
        opset_version=17,
        dynamo=False,
    )
