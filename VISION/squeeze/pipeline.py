"""The optimisation pipeline: teacher -> pruning / distillation / quantization, with accuracy
verified on the exported artifact after every single step.

Each step is cached in the run directory under a name; re-running resumes where it stopped and
re-running a finished run changes nothing. The run id is a hash of the configuration and the
dataset version, so the same inputs always land in the same place.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import data, quant, stats

logger = logging.getLogger("squeeze.pipeline")


@dataclass(frozen=True)
class Config:
    size: int = 160
    pretrained: bool = True
    seed: int = 0
    n_train: int = 4000  # images the students are fine-tuned on (teacher logits are cached for these)
    n_prune_ft: int = 1500  # images a pruned teacher is fine-tuned on
    n_dev: int = 400  # dev images used for every choice made during quantization
    n_calib: int = 128
    prune_ratios: tuple[float, ...] = (0.3, 0.5)
    student_epochs: int = 3
    student_lr: float = 0.005
    prune_epochs: int = 2
    prune_lr: float = 0.01
    temperature: float = 4.0
    alpha: float = 0.7
    # The accuracy budget: a variant may ship if it is at most this far below the teacher on eval.
    budget_pt: float = 2.0
    # Mixed precision: smallest number of float layers that keeps dev top-1 within this of fp32.
    mixed_tol_pt: float = 1.0
    mixed_ks: tuple[int, ...] = (0, 1, 2, 3, 4, 6, 8)
    sens_max_layers: int | None = None  # smoke tests scan only the first few layers


SMOKE = Config(
    size=64,
    pretrained=False,
    n_train=48,
    n_prune_ft=32,
    n_dev=20,
    n_calib=16,
    prune_ratios=(0.5,),
    student_epochs=1,
    prune_epochs=1,
    budget_pt=100.0,
    mixed_tol_pt=100.0,
    mixed_ks=(0, 1),
    sens_max_layers=2,
)


def _git_commit() -> str:
    try:
        root = Path(__file__).resolve().parent
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            check=True,
        )
        return head.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Run:
    def __init__(self, cfg: Config, ds: data.Dataset, runs_dir: Path) -> None:
        self.cfg, self.ds = cfg, ds
        ident = json.dumps([dataclasses.asdict(cfg), ds.version], sort_keys=True)
        self.run_id = hashlib.sha256(ident.encode()).hexdigest()[:12]
        self.dir = runs_dir / self.run_id
        (self.dir / "steps").mkdir(parents=True, exist_ok=True)
        (self.dir / "logits").mkdir(exist_ok=True)
        self.eval_idx = ds.idx("eval")
        self.dev_idx = ds.idx("dev", cfg.n_dev)
        self.train_idx = ds.idx("train", cfg.n_train)
        self.y_eval = ds.labels[self.eval_idx]
        calib = ds.idx("calib", cfg.n_calib)
        half = len(calib) // 2
        # Calibration that represents deployment: half as the dataset has them, half as the
        # camera would deliver them.
        self.calib_clean = np.asarray(ds.images[calib])
        self.calib_deploy = data.deploy_conditions(self.calib_clean, cfg.seed + 1)
        self.calib = np.concatenate([self.calib_clean[:half], self.calib_deploy[half:]])
        self.eval_deploy = self._cached_array(
            "eval_deploy.npy", lambda: data.deploy_conditions(np.asarray(ds.images[self.eval_idx]), cfg.seed)
        )
        self.variants: dict[str, dict[str, Any]] = {}

    def _cached_array(self, name: str, make: Callable[[], np.ndarray]) -> np.ndarray:
        path = self.dir / name
        if not path.exists():
            np.save(path, make())
        out: np.ndarray = np.load(path)
        return out

    def step(self, name: str, make: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run `make` once; afterwards return what it returned. A crash mid-step leaves no record."""
        path = self.dir / "steps" / f"{name}.json"
        if path.exists():
            out: dict[str, Any] = json.loads(path.read_text())
            logger.info("step %s: cached", name)
        else:
            start = time.time()
            out = make()
            out["seconds"] = round(time.time() - start, 1)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, indent=1))
            tmp.replace(path)
            logger.info("step %s: done in %.0fs", name, out["seconds"])
        return out

    def verify(
        self,
        name: str,
        *,
        parent: str | None,
        technique: str,
        arch: str,
        precision: str,
        params: int,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Accuracy of the ONNX file that would ship, measured now, compared with its parent on the
        same images, and checked against the accuracy budget."""
        path = self.dir / f"{name}.onnx"
        lg = quant.logits(path, self.ds.images, self.eval_idx)
        np.save(self.dir / "logits" / f"{name}.npy", lg)
        lg_deploy = quant.logits(path, self.eval_deploy, np.arange(len(self.eval_deploy)))
        acc, acc_deploy = stats.accuracy(lg, self.y_eval), stats.accuracy(lg_deploy, self.y_eval)
        out: dict[str, Any] = {
            "name": name,
            "parent": parent,
            "technique": technique,
            "arch": arch,
            "precision": precision,
            "params": params,
            "file": path.name,
            "sha256": sha256(path),
            "size_bytes": path.stat().st_size,
            "eval": acc,
            "eval_deploy": acc_deploy,
            "vs_parent": None,
            "detail": detail or {},
        }
        if parent:
            ref = np.load(self.dir / "logits" / f"{parent}.npy")
            delta, lo, hi = stats.paired_delta(ref.argmax(1) == self.y_eval, lg.argmax(1) == self.y_eval)
            out["vs_parent"] = {
                "agree": float((ref.argmax(1) == lg.argmax(1)).mean()),
                "delta": delta,
                "lo": lo,
                "hi": hi,
            }
        floor = self.teacher_top1(acc) - self.cfg.budget_pt / 100
        out["gate"] = "pass" if acc["top1"] >= floor - 1e-9 else "fail"
        out["gate_reason"] = (
            f"top-1 {acc['top1']:.3f} vs floor {floor:.3f} (teacher - {self.cfg.budget_pt} pt)"
        )
        logger.info(
            "verify %s: top1 %.3f deploy %.3f gate %s", name, acc["top1"], acc_deploy["top1"], out["gate"]
        )
        return out

    def teacher_top1(self, own: dict[str, Any]) -> float:
        t = self.variants.get("teacher-r50")
        return float(t["eval"]["top1"]) if t else float(own["top1"])

    def add(self, name: str, make: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        self.variants[name] = self.step(name, make)
        return self.variants[name]


def _baselines(run: Run) -> list[dict[str, Any]]:
    """What you get with no deep model at all. Everything else is reported against these."""
    import torch

    ds, cfg = run.ds, run.cfg
    y_train = ds.labels[run.train_idx]
    majority = int(np.bincount(y_train, minlength=len(data.CLASSES)).argmax())
    out = [
        {
            "name": "majority class",
            **stats.accuracy(np.eye(len(data.CLASSES))[[majority] * len(run.y_eval)], run.y_eval),
        }
    ]

    def features(idx: np.ndarray) -> torch.Tensor:  # 8x8 mean-pooled pixels
        x = np.asarray(ds.images[np.sort(idx)], dtype=np.float32) / 255.0
        k = cfg.size // 8
        return torch.from_numpy(x.reshape(len(x), 8, k, 8, k, 3).mean((2, 4)).reshape(len(x), -1))

    torch.manual_seed(cfg.seed)
    x, y = features(run.train_idx), torch.from_numpy(y_train)
    linear = torch.nn.Linear(x.shape[1], len(data.CLASSES))
    opt = torch.optim.LBFGS(linear.parameters(), max_iter=200)

    def closure() -> torch.Tensor:
        opt.zero_grad()
        loss: torch.Tensor = (
            torch.nn.functional.cross_entropy(linear(x), y) + 1e-3 * linear.weight.pow(2).sum()
        )
        loss.backward()
        return loss

    opt.step(closure)
    with torch.no_grad():
        lg = linear(features(run.eval_idx)).numpy()
    out.append({"name": "logistic regression on 8x8 pixels", **stats.accuracy(lg, run.y_eval)})
    return out


def execute(cfg: Config, ds: data.Dataset, runs_dir: Path) -> dict[str, Any]:
    """Run (or resume) the whole pipeline and return the run record."""
    import torch

    from . import models

    torch.manual_seed(cfg.seed)
    run = Run(cfg, ds, runs_dir)
    images, labels = ds.images, ds.labels

    def export_and_verify(net: Any, name: str, **kw: Any) -> dict[str, Any]:
        path = run.dir / f"{name}.onnx"
        models.export(net, path, cfg.size)
        probe = run.eval_idx[:16]
        diff = np.abs(models.predict(net, images, probe) - quant.logits(path, images, probe)).max()
        detail = {**kw.pop("detail", {}), "export_max_abs_logit_diff": float(diff)}
        return run.verify(name, params=models.n_params(net), detail=detail, **kw)

    # --- teacher and baselines ----------------------------------------------------------------
    run.add(
        "teacher-r50",
        lambda: export_and_verify(
            models.teacher(cfg.pretrained),
            "teacher-r50",
            parent=None,
            technique="none",
            arch="resnet50",
            precision="fp32",
        ),
    )
    teacher_onnx = run.dir / "teacher-r50.onnx"
    baselines = run.step("baselines", lambda: {"rows": _baselines(run)})["rows"]
    soft = run._cached_array(
        "teacher_train_logits.npy", lambda: quant.logits(teacher_onnx, images, run.train_idx)
    )

    def progress(tag: str) -> Callable[[int, int, float], None]:
        return lambda step, total, loss: logger.info("%s step %d/%d loss %.4f", tag, step, total, loss)

    # --- distillation -------------------------------------------------------------------------
    run.add(
        "student-pretrained",
        lambda: export_and_verify(
            models.student(cfg.pretrained),
            "student-pretrained",
            parent=None,
            technique="architecture",
            arch="mobilenet_v3_small",
            precision="fp32",
        ),
    )

    def tuned(name: str, kd: bool) -> dict[str, Any]:
        net = models.student(cfg.pretrained)
        history = models.train(
            net,
            images,
            labels,
            run.train_idx,
            teacher_logits=soft if kd else None,
            epochs=cfg.student_epochs,
            lr=cfg.student_lr,
            temperature=cfg.temperature,
            alpha=cfg.alpha,
            seed=cfg.seed,
            progress=progress(name),
        )
        return export_and_verify(
            net,
            name,
            parent="teacher-r50" if kd else "student-pretrained",
            technique="distillation" if kd else "finetune",
            arch="mobilenet_v3_small",
            precision="fp32",
            detail={"loss_per_epoch": history, "n_train": len(run.train_idx)},
        )

    run.add("student-ce", lambda: tuned("student-ce", kd=False))
    run.add("student-kd", lambda: tuned("student-kd", kd=True))

    # --- structured pruning -------------------------------------------------------------------
    ft_idx = run.train_idx[: cfg.n_prune_ft]
    for ratio in cfg.prune_ratios:
        tag = f"r50-prune{round(ratio * 100)}"
        arch = f"resnet50-pruned{round(ratio * 100)}"

        def raw(tag: str = tag, arch: str = arch, ratio: float = ratio) -> dict[str, Any]:
            net = models.prune_bottlenecks(models.teacher(cfg.pretrained), ratio)
            return export_and_verify(
                net, f"{tag}-raw", parent="teacher-r50", technique="pruning", arch=arch, precision="fp32"
            )

        def recovered(tag: str = tag, arch: str = arch, ratio: float = ratio) -> dict[str, Any]:
            net = models.prune_bottlenecks(models.teacher(cfg.pretrained), ratio)
            history = models.train(
                net,
                images,
                labels,
                ft_idx,
                teacher_logits=soft[: len(ft_idx)],
                epochs=cfg.prune_epochs,
                lr=cfg.prune_lr,
                batch=16,
                temperature=cfg.temperature,
                alpha=cfg.alpha,
                seed=cfg.seed,
                progress=progress(tag),
            )
            return export_and_verify(
                net,
                tag,
                parent=f"{tag}-raw",
                technique="pruning+finetune",
                arch=arch,
                precision="fp32",
                detail={"loss_per_epoch": history, "n_train": len(ft_idx), "ratio": ratio},
            )

        run.add(f"{tag}-raw", raw)
        run.add(tag, recovered)

    # --- quantization -------------------------------------------------------------------------
    dev_u8, dev_y = np.asarray(images[run.dev_idx]), labels[run.dev_idx]

    def prepared(name: str) -> Path:
        path = run.dir / f"{name}.prepared.onnx"
        if not path.exists():
            quant.prepare(run.dir / f"{name}.onnx", path)
        return path

    def int8(
        src: str, name: str, exclude: list[str], technique: str, detail: dict[str, Any]
    ) -> dict[str, Any]:
        quant.quantize(prepared(src), run.dir / f"{name}.onnx", run.calib, exclude=exclude)
        parent = run.variants[src]
        return run.verify(
            name,
            parent=src,
            technique=technique,
            arch=parent["arch"],
            precision="int8" if not exclude else "int8+fp32",
            params=parent["params"],
            detail={
                **detail,
                "calibration": {"n": len(run.calib), "mix": "half clean, half deploy conditions"},
            },
        )

    for src in ["teacher-r50", *(f"r50-prune{round(r * 100)}" for r in cfg.prune_ratios)]:
        run.add(
            f"{src.replace('teacher-', '')}-int8",
            lambda src=src: int8(src, f"{src.replace('teacher-', '')}-int8", [], "quantization", {}),  # type: ignore[misc]
        )
    run.add("student-kd-int8", lambda: int8("student-kd", "student-kd-int8", [], "quantization", {}))

    def scan() -> dict[str, Any]:
        rows = quant.sensitivity(prepared("student-kd"), run.calib, dev_u8, dev_y, limit=cfg.sens_max_layers)
        ref = quant.logits(prepared("student-kd"), dev_u8, np.arange(len(dev_u8)))
        ref_acc = float((ref.argmax(1) == dev_y).mean())
        search, chosen = [], None
        tmp = run.dir / "search.onnx"
        for k in cfg.mixed_ks:
            quant.quantize(prepared("student-kd"), tmp, run.calib, exclude=[r["node"] for r in rows[:k]])
            got = quant.logits(tmp, dev_u8, np.arange(len(dev_u8)))
            acc = float((got.argmax(1) == dev_y).mean())
            search.append(
                {"k": k, "dev_top1": acc, "dev_kl": quant.kl(ref, got), "size_bytes": tmp.stat().st_size}
            )
            logger.info("mixed search k=%d dev top1 %.3f (fp32 %.3f)", k, acc, ref_acc)
            if acc >= ref_acc - cfg.mixed_tol_pt / 100 - 1e-9:
                chosen = k
                break
        tmp.unlink(missing_ok=True)
        return {
            "variant": "student-kd",
            "n_dev": len(dev_u8),
            "dev_top1_fp32": ref_acc,
            "rows": rows,
            "search": search,
            # None: no k in the list met the tolerance. The largest is used and the gate decides.
            "chosen_k": chosen,
        }

    sens = run.step("sensitivity-student-kd", scan)
    k = sens["chosen_k"] if sens["chosen_k"] is not None else cfg.mixed_ks[-1]
    float_layers = [r["node"] for r in sens["rows"][:k]]
    for row in sens["rows"]:
        row["kept_float"] = row["node"] in float_layers
    run.add(
        "student-kd-int8-mixed",
        lambda: int8(
            "student-kd",
            "student-kd-int8-mixed",
            float_layers,
            "quantization-mixed",
            {"float_layers": float_layers},
        ),
    )

    def calibration_study() -> dict[str, Any]:
        """Same model, same float layers, different calibration data. Evaluated on eval, clean and
        under deployment conditions."""
        rng = np.random.default_rng(cfg.seed)
        noise = rng.integers(0, 256, run.calib_clean.shape, dtype=np.uint8)
        n = len(run.calib_clean)
        sets = [
            ("uniform noise", noise, "minmax"),
            ("clean", run.calib_clean[: max(2, n // 16)], "minmax"),
            ("clean", run.calib_clean[: max(4, n // 4)], "minmax"),
            ("clean", run.calib_clean, "minmax"),
            ("deploy conditions", run.calib_deploy, "minmax"),
            ("half clean, half deploy", run.calib, "minmax"),
            ("half clean, half deploy", run.calib, "entropy"),
        ]
        rows, tmp = [], run.dir / "calib.onnx"
        for source, x, method in sets:
            quant.quantize(prepared("student-kd"), tmp, x, method=method, exclude=float_layers)
            clean = stats.accuracy(quant.logits(tmp, images, run.eval_idx), run.y_eval)
            deploy = stats.accuracy(
                quant.logits(tmp, run.eval_deploy, np.arange(len(run.eval_deploy))), run.y_eval
            )
            rows.append(
                {"source": source, "n": len(x), "method": method, "eval": clean, "eval_deploy": deploy}
            )
            logger.info(
                "calibration %s n=%d %s: %.3f / %.3f", source, len(x), method, clean["top1"], deploy["top1"]
            )
        tmp.unlink(missing_ok=True)
        return {"rows": rows}

    study = run.step("calibration-study", calibration_study)["rows"]

    import onnx
    import onnxruntime
    import torchvision

    record = {
        "schema": 1,
        "kind": "pipeline",
        "run_id": run.run_id,
        "created_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime((run.dir / "steps" / "teacher-r50.json").stat().st_mtime)
        ),
        "git_commit": _git_commit(),
        "config": dataclasses.asdict(cfg),
        "dataset": {"version": ds.version, "profile": ds.profile},
        "versions": {
            "torch": torch.__version__,
            "torchvision": torchvision.__version__,
            "onnx": onnx.__version__,
            "onnxruntime": onnxruntime.__version__,
        },
        "baselines": baselines,
        "variants": list(run.variants.values()),
        "sensitivity": sens,
        "calibration_study": study,
    }
    (run.dir / "run.json").write_text(json.dumps(record, indent=1))
    return record
