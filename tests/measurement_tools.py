"""Explicit test-only tool imports, independent of pytest's executable entrypoint."""

from tests.test_measurement_evidence import load_tool

DEPENDENCIES = {
    "measurement_cancel_validation": ("measurement_cancel", "measurement_sample_validation"),
    "measurement_observation": (
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
