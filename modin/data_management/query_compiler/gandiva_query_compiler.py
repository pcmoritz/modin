import numpy as np
from .pandas_query_compiler import PandasQueryCompiler
import pyarrow as pa
import pandas
import ray
from pandas.core.computation.expr import Expr
from pandas.core.computation.scope import Scope
from pandas.core.computation.ops import UnaryOp, BinOp, Term, MathCall, Constant

from modin.error_message import ErrorMessage


class FakeSeries:
    def __init__(self, dtype):
        self.dtype = dtype

@ray.remote
def sum_pyarrow_table(table):
    names = []
    arrays = []
    for column in table.columns:
        if pa.types.is_floating(column.type) or pa.types.is_integer(column.type):
            names.append(column.name)
            value = sum([chunk.sum().as_py() for chunk in column.data.chunks])
            arrays.append(pa.array([value], type=column.type))
    return pa.Table.from_arrays(arrays, names=names)
    # return arrays, names

# @ray.remote
# def sum_block(arrow_table):
#     for column in arrow_table.columns:
#     return sum([chunk.sum().as_py() for chunk in arrow_table[15].data.chunks])

class GandivaQueryCompiler(PandasQueryCompiler):
    def query(self, expr, **kwargs):
        """Query columns of the DataManager with a boolean expression.

        Args:
            expr: Boolean expression to query the columns with.

        Returns:
            DataManager containing the rows where the boolean expression is satisfied.
        """

        def gen_table_expr(table, expr):
            resolver = {
                name: FakeSeries(dtype.to_pandas_dtype())
                for name, dtype in zip(table.schema.names, table.schema.types)
            }
            scope = Scope(level=0, resolvers=(resolver,))
            return Expr(expr=expr, env=scope)

        import pyarrow.gandiva as gandiva

        unary_ops = {"~": "not"}
        math_calls = {"log": "log", "exp": "exp", "log10": "log10", "cbrt": "cbrt"}
        bin_ops = {
            "+": "add",
            "-": "subtract",
            "*": "multiply",
            "/": "divide",
            "**": "power",
        }
        cmp_ops = {
            "==": "equal",
            "!=": "not_equal",
            ">": "greater_than",
            "<": "less_than",
            "<=": "less_than_or_equal_to",
            ">": "greater_than",
            ">=": "greater_than_or_equal_to",
            "like": "like",
        }

        def build_node(table, terms, builder):
            if isinstance(terms, Constant):
                return builder.make_literal(
                    terms.value, (pa.from_numpy_dtype(terms.return_type))
                )

            if isinstance(terms, Term):
                return builder.make_field(table.schema.field_by_name(terms.name))

            if isinstance(terms, BinOp):
                lnode = build_node(table, terms.lhs, builder)
                rnode = build_node(table, terms.rhs, builder)
                return_type = pa.from_numpy_dtype(terms.return_type)

                if terms.op == "&":
                    return builder.make_and([lnode, rnode])
                if terms.op == "|":
                    return builder.make_or([lnode, rnode])
                if terms.op in cmp_ops:
                    assert return_type == pa.bool_()
                    return builder.make_function(
                        cmp_ops[terms.op], [lnode, rnode], return_type
                    )
                if terms.op in bin_ops:
                    return builder.make_function(
                        bin_ops[terms.op], [lnode, rnode], return_type
                    )

            if isinstance(terms, UnaryOp):
                return_type = pa.from_numpy_dtype(terms.return_type)
                return builder.make_function(
                    unary_ops[terms.op],
                    [build_node(table, terms.operand, builder)],
                    return_type,
                )

            if isinstance(terms, MathCall):
                return_type = pa.from_numpy_dtype(terms.return_type)
                childern = [
                    build_node(table, child, builder) for child in terms.operands
                ]
                return builder.make_function(
                    math_calls[terms.op], childern, return_type
                )

            raise TypeError("Unsupported term type: %s" % terms)

        def can_be_condition(expr):
            if isinstance(expr.terms, BinOp):
                if expr.terms.op in cmp_ops or expr.terms.op in ("&", "|"):
                    return True
            elif isinstance(expr.terms, UnaryOp):
                if expr.terms.op == "~":
                    return True
            return False

        def filter_with_selection_vector(table, s):
            record_batch = table.to_batches()[0]
            indices = s.to_array()
            new_columns = [
                    pa.lib.take(c, indices) for c in record_batch.columns]
            return pa.Table.from_arrays(new_columns, record_batch.schema.names)

        def gandiva_query(table, query):
            expr = gen_table_expr(table, query)
            if not can_be_condition(expr):
                raise ValueError("Root operation should be a filter.")
            builder = gandiva.TreeExprBuilder()
            root = build_node(table, expr.terms, builder)
            cond = builder.make_condition(root)
            filt = gandiva.make_filter(table.schema, cond)
            sel_vec = filt.evaluate(table.to_batches()[0], pa.default_memory_pool())
            result = filter_with_selection_vector(table, sel_vec)
            return result

        def gandiva_query2(table, query):
            expr = gen_table_expr(table, query)
            if not can_be_condition(expr):
                raise ValueError("Root operation should be a filter.")
            builder = gandiva.TreeExprBuilder()
            root = build_node(table, expr.terms, builder)
            cond = builder.make_condition(root)
            filt = gandiva.make_filter(table.schema, cond)
            return filt

        def query_builder(arrow_table, **kwargs):
            return gandiva_query(arrow_table, kwargs.get("expr", ""))

        kwargs["expr"] = expr
        func = self._prepare_method(query_builder, **kwargs)
        new_data = self._map_across_full_axis(1, func)
        # Query removes rows, so we need to update the index
        new_index = self.compute_index(0, new_data, False)
        return self.__constructor__(
            new_data, new_index, self.columns, self._dtype_cache
        )

    def sum(self, **kwargs):

        new_partitions = np.array(
            [
                [sum_pyarrow_table.remote(part.oid) for part in row_of_parts]
                for row_of_parts in self.data.partitions
            ]
        )
        sum_partitions = ray.get(list(new_partitions[:,0]))
        names = sum_partitions[0].schema.names
        accumulator = {name: 0.0 for name in names}
        for i, name in enumerate(names):
            for sum_partition in sum_partitions:
                    accumulator[name] += sum_partition.columns[i].data[0].as_py()

        arrays = []
        for name in names:
            arrays.append(pa.array([accumulator[name]], type=sum_partitions[0].schema.field_by_name(name)))

        return pa.Table.from_arrays(arrays, names=names)




    def compute_index(self, axis, data_object, compute_diff=True):
        def arrow_index_extraction(table, axis):
            if not axis:
                return pandas.Index(table.column(table.num_columns - 1))
            else:
                try:
                    return pandas.Index(table.columns)
                except AttributeError:
                    return []

        index_obj = self.index if not axis else self.columns
        old_blocks = self.data if compute_diff else None
        new_indices = data_object.get_indices(
            axis=axis,
            index_func=lambda df: arrow_index_extraction(df, axis),
            old_blocks=old_blocks,
        )
        return index_obj[new_indices] if compute_diff else new_indices

    def to_pandas(self):
        """Converts Modin DataFrame to Pandas DataFrame.

        Returns:
            Pandas DataFrame of the DataManager.
        """
        df = self.data.to_pandas(is_transposed=self._is_transposed)
        if df.empty:
            dtype_dict = {
                col_name: pandas.Series(dtype=self.dtypes[col_name])
                for col_name in self.columns
            }
            df = pandas.DataFrame(dtype_dict, self.index)
        else:
            ErrorMessage.catch_bugs_and_request_email(
                len(df.index) != len(self.index) or len(df.columns) != len(self.columns)
            )
            df.index = self.index
            df.columns = self.columns
        return df

    def getitem_column_array(self, key):

        numeric_indices = list(self.columns.get_indexer_for(key))

        def getitem(table, internal_indices=[]):
            # print("internal_indices", internal_indices)
            # return table.drop([list(table.itercolumns())[i] for i in range(len(list(table.itercolumns()))) if i not in internal_indices)
            result = pa.Table.from_arrays([table.column(i) for i in internal_indices])
            # print("result", result.to_pandas())
            return result

        result = self.data.apply_func_to_select_indices(0, getitem, numeric_indices, keep_remaining=False)
        new_columns = self.columns[numeric_indices]
        # new_dtypes = self.dtypes[numeric_indices]
        return self.__constructor__(result, self.index, new_columns) # , new_dtypes)