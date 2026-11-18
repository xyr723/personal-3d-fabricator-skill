from importlib.util import find_spec

import numpy as np
import pytest

from personal_3d_fabricator.connectors import (
    MagnetPocketSpec, PinSocketSpec, ScrewInsertSpec,
    build_magnet_pocket, build_pin_socket_pair, build_screw_insert,
)


@pytest.mark.parametrize("args", [(0, 12, 0.25, 2), (6, float("inf"), 0.25, 2),
                                  (6, 12, -0.1, 2), (6, 12, 0.25, 0)])
def test_pin_spec_validates_dimensions(args):
    with pytest.raises(ValueError):
        PinSocketSpec(*args)


@pytest.mark.skipif(find_spec("cadquery") is None, reason="cadquery extra not installed")
def test_solids_and_local_transforms():
    pair = build_pin_socket_pair(PinSocketSpec(6, 12, 0.25, 2))
    assert pair.socket_diameter_mm == pytest.approx(6.5)
    assert pair.pin.BoundingBox().xlen == pytest.approx(6)
    assert pair.socket.BoundingBox().xlen == pytest.approx(6.5)
    assert len(pair.placement_transforms) == 2
    for solid in (pair.pin, pair.socket):
        assert solid.BoundingBox().zmin == pytest.approx(0)
        assert solid.BoundingBox().zmax == pytest.approx(12)
    second = np.eye(4)
    second[0, 3] = 13.0
    assert np.array_equal(pair.placement_transforms, (np.eye(4), second))
    for tool in (build_magnet_pocket(MagnetPocketSpec(8, 3)),
                 build_screw_insert(ScrewInsertSpec(8, 3))):
        assert tool.solid.BoundingBox().zlen == pytest.approx(3)
        assert tool.solid.BoundingBox().xlen == pytest.approx(8)
        assert np.array_equal(tool.placement_transform, np.eye(4))
