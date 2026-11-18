"""Small meshes generated during inspection tests."""

import numpy as np
import trimesh


def box():
    return trimesh.creation.box(extents=(20.0, 30.0, 40.0))


def open_box():
    mesh = box()
    mesh.update_faces(np.arange(len(mesh.faces) - 1))
    return mesh
