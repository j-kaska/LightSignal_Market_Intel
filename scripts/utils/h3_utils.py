import h3 as _h3
import importlib.metadata

try:
    _version_str = _h3.__version__
except AttributeError:
    _version_str = importlib.metadata.version('h3')

_H3_VERSION = tuple(int(x) for x in _version_str.split('.')[:2])
_IS_V4 = hasattr(_h3, 'latlng_to_cell')


def latlng_to_cell(lat, lon, resolution):
    if _IS_V4:
        return _h3.latlng_to_cell(lat, lon, resolution)
    else:
        return _h3.geo_to_h3(lat, lon, resolution)


def cell_to_boundary(h3_id):
    if _IS_V4:
        return list(_h3.cell_to_boundary(h3_id))
    else:
        return list(_h3.h3_to_geo_boundary(h3_id))


def cell_to_parent(h3_id, resolution):
    if _IS_V4:
        return _h3.cell_to_parent(h3_id, resolution)
    else:
        return _h3.h3_to_parent(h3_id, resolution)


def h3_version():
    return _version_str
