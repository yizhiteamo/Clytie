"""Explicit test-only tool imports, independent of pytest's executable entrypoint."""

from tests.test_measurement_evidence import load_tool

DEPENDENCIES = {
    "measurement_preparation_request": ("measurement_preparation_transport",),
    "measurement_analysis_index": (
        "measurement_evidence",
        "measurement_cancel",
        "measurement_control_observation",
        "measurement_io_observation",
        "measurement_pressure_observation",
    ),
    "measurement_mechanism_validation": (
        "measurement_analysis_index",
        "measurement_sample_validation",
        "measurement_io_observation",
        "measurement_pressure_observation",
    ),
    "measurement_preparation_generate": ("measurement_preparation_transport",),
    "measurement_preparation_transport": (
        "measurement_launch",
        "measurement_preparation_evidence",
        "measurement_transport",
        "measurement_version",
    ),
    "measurement_preparation_evidence": ("measurement_plan",),
    "measurement_cancel_validation": ("measurement_cancel", "measurement_sample_validation"),
    "measurement_observation": (
        "measurement_control_observation",
        "measurement_io_observation",
        "measurement_pressure_observation",
        "measurement_native_observation",
    ),
    "measurement_geometry": ("measurement_fixture",),
    "measurement_trial": (
        "measurement_cancel",
        "measurement_recording",
        "measurement_load",
        "measurement_observation",
        "measurement_native_observation",
        "measurement_resources",
        "measurement_version",
        "measurement_views",
        "measurement_fixture",
    ),
    "measurement_launch": (
        "measurement_version",
        "measurement_control_observation",
        "measurement_io_observation",
        "measurement_pressure_observation",
        "measurement_sample_validation",
    ),
    "measurement_pressure_observation": ("measurement_io_observation",),
    "measurement_load": ("measurement_recording",),
    "measurement_views": ("measurement_recording",),
}


def load(name):
    for dependency in DEPENDENCIES.get(name, ()):
        load(dependency)
    return load_tool(name)
