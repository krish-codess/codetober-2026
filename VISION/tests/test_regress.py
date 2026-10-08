from squeeze import regress

BASELINE = {
    "reference": "student-kd",
    "accuracy": {"student-kd": 0.97, "student-kd-int8-mixed": 0.96},
    "latency_ratio": {"Cortex-A76": {"student-kd-int8-mixed": 0.60}},
}


def current(mixed_ms=6.0, mixed_top1=0.96, agree=1.0):
    return {
        "student-kd": {"top1": 0.97, "agree_host": 1.0, "p50_ms": 10.0},
        "student-kd-int8-mixed": {"top1": mixed_top1, "agree_host": agree, "p50_ms": mixed_ms},
    }


def test_unchanged_results_pass():
    assert regress.check(current(), BASELINE, "Cortex-A76", 0.3) == ([], [])


def test_a_latency_regression_is_caught_relative_to_the_reference_model():
    # the whole machine being slower moves both models and is not a regression
    slower_machine = {k: {**v, "p50_ms": v["p50_ms"] * 3} for k, v in current().items()}
    assert regress.check(slower_machine, BASELINE, "Cortex-A76", 0.3)[0] == []
    failures, _ = regress.check(current(mixed_ms=9.0), BASELINE, "Cortex-A76", 0.3)  # 0.9x vs 0.6x baseline
    assert len(failures) == 1 and "0.90x" in failures[0]
    assert regress.check(current(mixed_ms=7.5), BASELINE, "Cortex-A76", 0.3)[0] == []  # +25%: within tolerance


def test_an_accuracy_drop_or_divergence_from_the_host_is_caught():
    assert "top-1 0.930" in regress.check(current(mixed_top1=0.93), BASELINE, "Cortex-A76", 0.3)[0][0]
    assert regress.check(current(mixed_top1=0.95), BASELINE, "Cortex-A76", 0.3)[0] == []  # one image: noise
    assert "match the build host" in regress.check(current(agree=0.9), BASELINE, "Cortex-A76", 0.3)[0][0]


def test_a_missing_model_fails_and_an_unknown_cpu_gates_accuracy_only():
    partial = {"student-kd": current()["student-kd"]}
    assert "not benchmarked" in regress.check(partial, BASELINE, "Cortex-A76", 0.3)[0][0]
    failures, notes = regress.check(current(mixed_ms=50.0), BASELINE, "EPYC 7763", 0.3)
    assert failures == [] and "no latency baseline" in notes[0]
    assert regress.check(current(mixed_top1=0.5), BASELINE, "EPYC 7763", 0.3)[0]  # accuracy still gated


def test_summarise_takes_the_median_run():
    def result(p50):
        return {"variant": "m", "accuracy": {"top1": 0.9, "agree_host": 1.0}, "latency_ms": {"p50": p50}}

    assert regress.summarise([result(5.0), result(50.0), result(6.0)])["m"]["p50_ms"] == 6.0
