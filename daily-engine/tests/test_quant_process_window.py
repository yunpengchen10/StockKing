"""Forecast fitting must not spawn a console for LightGBM CPU discovery."""
from __future__ import annotations

import numpy as np
import pytest

import src.quant.service as quant_service


@pytest.mark.parametrize("cpu_count, expected", [(None, 1), (1, 1), (4, 2), (64, 8)])
def test_forecast_sets_a_positive_bounded_thread_count(monkeypatch, cpu_count, expected):
    pytest.importorskip("lightgbm")
    monkeypatch.setattr(quant_service.os, "cpu_count", lambda: cpu_count)

    model = quant_service.make_lightgbm_factory(5)()

    assert model.n_jobs == expected


def test_forecast_fit_and_predict_do_not_probe_cpu_with_a_subprocess(monkeypatch):
    lightgbm = pytest.importorskip("lightgbm.sklearn")

    def unexpected_cpu_probe(*args, **kwargs):
        raise AssertionError("Forecast must not invoke joblib's PowerShell/WMIC CPU probe")

    monkeypatch.setattr(lightgbm, "_LGBMCpuCount", unexpected_cpu_probe)
    model = quant_service.make_lightgbm_factory(1)()
    model.set_params(n_estimators=2)
    rng = np.random.default_rng(20261014)
    features = rng.normal(size=(80, 3))
    target = features[:, 0] * 0.02 - features[:, 1] * 0.01

    model.fit(features, target)
    prediction = model.predict(features[:2])

    assert prediction.shape == (2,)
    assert np.isfinite(prediction).all()
