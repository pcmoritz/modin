from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from io import BytesIO

import ray
import pandas
import pyarrow
import pyarrow.csv

from modin.backends.pyarrow.query_compiler import PyarrowQueryCompiler
from modin.engines.ray.generic.io import RayIO
from modin.experimental.engines.pyarrow_on_ray.frame.partition_manager import (
    PyarrowOnRayFrameManager,
)
from modin.experimental.engines.pyarrow_on_ray.frame.partition import (
    PyarrowOnRayFramePartition,
)


@ray.remote
def _read_csv_with_offset_pyarrow_on_ray(
    fname, num_splits, start, end, kwargs, header
):  # pragma: no cover
    """Use a Ray task to read a chunk of a CSV into a pyarrow Table.
     Note: Ray functions are not detected by codecov (thus pragma: no cover)
     Args:
        fname: The filename of the file to open.
        num_splits: The number of splits (partitions) to separate the DataFrame into.
        start: The start byte offset.
        end: The end byte offset.
        kwargs: The kwargs for the Pandas `read_csv` function.
        header: The header of the file.
     Returns:
         A list containing the split Pandas DataFrames and the Index as the last
            element. If there is not `index_col` set, then we just return the length.
            This is used to determine the total length of the DataFrame to build a
            default Index.
    """
    bio = open(fname, "rb")
    # The header line for the CSV file
    first_line = bio.readline()
    bio.seek(start)
    to_read = header + first_line + bio.read(end - start)
    bio.close()
    pandas_df = pyarrow.csv.read_csv(
        BytesIO(to_read),
        parse_options=pyarrow.csv.ParseOptions(header_rows=1))
    return [pandas_df] + [pyarrow.Table.from_pandas(pandas.DataFrame()) for _ in range(num_splits - 1)] + [len(pandas_df)]


class PyarrowOnRayIO(RayIO):

    frame_mgr_cls = PyarrowOnRayFrameManager
    frame_partition_cls = PyarrowOnRayFramePartition
    query_compiler_cls = PyarrowQueryCompiler

    read_parquet_remote_task = None
    read_csv_remote_task = _read_csv_with_offset_pyarrow_on_ray
    read_hdf_remote_task = None
    read_feather_remote_task = None
    read_sql_remote_task = None
