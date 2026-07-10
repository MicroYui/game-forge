"""M3a Task 5: statistical power for the seeded bench (design §4).

Turns the PRD §13.4 power target — per-defect-class sample size so BDR's 95%
Wilson CI half-width ≤ ±5% — into a computable `required_n`, and reports the
half-width actually achieved so under-powered classes are visible (not hidden).
"""
from gameforge.bench.power import PowerRow, achieved_half_width, required_n
from gameforge.bench.taxonomy import DefectClass


def test_required_n_conservative_at_half():
    n = required_n(0.5)  # worst-case proportion → largest n
    assert 375 <= n <= 395  # ~384 for ±5% at 95%


def test_required_n_smaller_for_high_bdr():
    assert required_n(0.95) < required_n(0.5)
    assert required_n(0.99) < required_n(0.95)


def test_achieved_half_width_shrinks_with_n():
    prev = achieved_half_width(5, 10)
    for n in (20, 50, 100, 400):
        hw = achieved_half_width(round(0.5 * n), n)
        assert hw < prev
        prev = hw


def test_required_n_actually_meets_the_target():
    for p in (0.5, 0.8, 0.95):
        n = required_n(p)
        assert achieved_half_width(round(p * n), n) <= 0.05
        # n-1 does NOT meet it (it is the SMALLEST such n)
        if n > 1:
            assert achieved_half_width(round(p * (n - 1)), n - 1) > 0.05


def test_power_row_target_met_flag():
    good = PowerRow(defect_class=DefectClass.dangling_reference, n=400,
                    achieved_half_width=0.04, target_met=True)
    assert good.target_met
    bad = PowerRow(defect_class=DefectClass.spoiler, n=20,
                   achieved_half_width=0.19, target_met=False)
    assert not bad.target_met
