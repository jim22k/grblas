from contextvars import ContextVar
from . import ffi
from .descriptor import lookup as descriptor_lookup
from .dtypes import libget, lookup_dtype
from .exceptions import check_status
from .expr import AmbiguousAssignOrExtract, Updater
from .mask import Mask
from .ops import UNKNOWN_OPCLASS, find_opclass, get_typed_op
from .unary import identity

NULL = ffi.NULL
CData = ffi.CData
recorder = ContextVar("recorder", default=None)


def call(cfunc_name, args):
    call_args = [getattr(x, "_carg", x) if x is not None else NULL for x in args]
    cfunc = libget(cfunc_name)
    err_code = cfunc(*call_args)
    check_status(err_code)
    rec = recorder.get()
    if rec is not None:
        rec.record(cfunc_name, args)
    return err_code


def _expect_type_message(
    self, x, types, *, within, argname=None, keyword_name=None, extra_message=""
):
    if type(types) is tuple:
        if type(x) in types:
            return
    elif type(x) is types:
        return
    if argname:
        argmsg = f"for argument `{argname}` "
    elif keyword_name:
        argmsg = f"for keyword argument `{keyword_name}=` "
    else:
        argmsg = ""
    if type(types) is tuple:
        expected = ", ".join(typ.__name__ for typ in types)
    else:
        expected = types.__name__
    if extra_message:
        extra_message = f"\n{extra_message}"
    return (
        f"Bad type {argmsg}in {type(self).__name__}.{within}(...).\n"
        f"    - Expected type: {expected}.\n"
        f"    - Got: {type(x)}."
        f"{extra_message}"
    )


def _expect_type(self, x, types, **kwargs):
    message = _expect_type_message(self, x, types, **kwargs)
    if message is not None:
        raise TypeError(message) from None


def _expect_op_message(
    self, op, values, *, within, argname=None, keyword_name=None, extra_message=""
):
    if type(values) is tuple:
        if op.opclass in values:
            return
    elif op.opclass == values:
        return
    if argname:
        argmsg = f"for argument `{argname}` "
    elif keyword_name:
        argmsg = f"for keyword argument `{keyword_name}=` "
    else:  # pragma: no cover
        argmsg = ""
    if type(values) is tuple:
        expected = ", ".join(values)
    else:
        expected = values
    if extra_message:
        extra_message = f"\n{extra_message}"
    return (
        f"Bad type {argmsg}in {type(self).__name__}.{within}(...).\n"
        f"    - Expected type: {expected}.\n"
        f"    - Got: {op.opclass}."
        f"{extra_message}"
    )


def _expect_op(self, op, values, **kwargs):
    message = _expect_op_message(self, op, values, **kwargs)
    if message is not None:
        raise TypeError(message) from None


def _check_mask(mask, output=None):
    if isinstance(mask, BaseType) or type(mask) is Mask:
        raise TypeError("Mask must indicate values (M.V) or structure (M.S)")
    if not isinstance(mask, Mask):
        raise TypeError(f"Invalid mask: {type(mask)}")
    if output is not None and type(mask.mask) is not type(output):
        raise TypeError(f"Mask object must be type {type(output)}; got {type(mask)}")


class BaseType:
    # Flag for operations which depend on scalar vs vector/matrix
    _is_scalar = False

    def __init__(self, gb_obj, dtype, name):
        if not isinstance(gb_obj, CData):
            raise TypeError("Object passed to __init__ must be CData type")
        self.gb_obj = gb_obj
        self.dtype = lookup_dtype(dtype)
        self.name = name

    def __call__(self, *optional_mask_and_accum, mask=None, accum=None, replace=False):
        # Pick out mask and accum from positional arguments
        mask_arg = None
        accum_arg = None
        for arg in optional_mask_and_accum:
            if isinstance(arg, (BaseType, Mask)):
                if self._is_scalar:
                    raise TypeError("Mask not allowed for Scalars")
                if mask_arg is not None:
                    raise TypeError("Got multiple values for argument 'mask'")
                mask_arg = arg
            else:
                if accum_arg is not None:
                    raise TypeError("Got multiple values for argument 'accum'")
                accum_arg, opclass = find_opclass(arg)
                if opclass == UNKNOWN_OPCLASS:
                    raise TypeError(f"Invalid item found in output params: {type(arg)}")
                if opclass != "BinaryOp":
                    raise TypeError(f"accum must be a BinaryOp, not {opclass}")
        # Merge positional and keyword arguments
        if mask_arg is not None and mask is not None:
            raise TypeError("Got multiple values for argument 'mask'")
        if mask_arg is not None:
            mask = mask_arg
        if mask is None:
            pass
        elif self._is_scalar:
            raise TypeError("Mask not allowed for Scalars")
        else:
            _check_mask(mask)
        if accum_arg is not None and accum is not None:
            raise TypeError("Got multiple values for argument 'accum'")
        if accum_arg is not None:
            accum = accum_arg
        return Updater(self, mask=mask, accum=accum, replace=replace)

    def __eq__(self, other):
        raise TypeError(
            f"__eq__ not defined for objects of type {type(self)}.  Use `.isequal` method instead."
        )

    def __lshift__(self, delayed):
        return self._update(delayed)

    def update(self, delayed):
        """
        Convenience function when no output arguments (mask, accum, replace) are used
        """
        return self._update(delayed)

    def _update(self, delayed, mask=None, accum=None, replace=False):
        # TODO: check expected output type (now included in Expression object)
        if not isinstance(delayed, BaseExpression):
            if type(delayed) is AmbiguousAssignOrExtract:
                if delayed.resolved_indexes.is_single_element and self._is_scalar:
                    # Extract element (s << v[1])
                    if accum is not None:
                        raise TypeError(
                            "Scalar accumulation with extract element"
                            "--such as `s(accum=accum) << v[0]`--is not supported"
                        )
                    self.value = delayed.new(dtype=self.dtype, name="s_extract").value
                    return

                # Extract (C << A[rows, cols])
                delayed = delayed._extract_delayed()
            elif type(delayed) is type(self):
                # Simple assignment (w << v)
                if self._is_scalar:
                    if accum is not None:
                        raise TypeError(
                            "Scalar update with accumulation--such as `s(accum=accum) << t`"
                            "--is not supported"
                        )
                    self.value = delayed.value
                    return

                delayed = delayed.apply(identity)
            elif self._is_scalar:
                if accum is not None:
                    raise TypeError(
                        "Scalar update with accumulation--such as `s(accum=accum) << t`"
                        "--is not supported"
                    )
                self.value = delayed
                return

            else:
                from .matrix import Matrix, TransposedMatrix, MatrixExpression

                if type(delayed) is TransposedMatrix and type(self) is Matrix:
                    # Transpose (C << A.T)
                    delayed = MatrixExpression(
                        "transpose",
                        "GrB_transpose",
                        [delayed],
                        expr_repr="{0}",
                        dtype=delayed.dtype,
                        nrows=delayed.nrows,
                        ncols=delayed.ncols,
                    )
                else:
                    from .scalar import Scalar

                    if type(delayed) is Scalar:
                        scalar = delayed
                    else:
                        try:
                            scalar = Scalar.from_value(delayed, name="")
                        except TypeError:
                            raise TypeError(
                                f"assignment value must be Expression object, not {type(delayed)}"
                            )
                    updater = self(
                        mask=mask,
                        accum=accum,
                        replace=replace,
                    )
                    if type(self) is Matrix:
                        if mask is None:
                            raise TypeError(
                                "Warning: updating a Matrix with a scalar without a mask will "
                                "make the Matrix dense.  This may use a lot of memory and probably "
                                "isn't what you want.  Perhaps you meant:"
                                "\n\n    M(M.S) << s\n\n"
                                "If you do wish to make a dense matrix, then please be explicit:"
                                "\n\n    M[:, :] = s"
                            )
                        updater[:, :] = scalar
                    else:  # Vector
                        updater[:] = scalar
                    return

        # Normalize mask and separate out complement and structural flags
        if mask is None:
            complement = False
            structure = False
        else:
            _check_mask(mask, self)
            complement = mask.complement
            structure = mask.structure

        # Normalize accumulator
        if accum is not None:
            accum = get_typed_op(accum, self.dtype)
            self._expect_op(accum, "BinaryOp", within="FIXME", keyword_name="accum")

        # Get descriptor based on flags
        desc = descriptor_lookup(
            transpose_first=delayed.at,
            transpose_second=delayed.bt,
            mask_complement=complement,
            mask_structure=structure,
            output_replace=replace,
        )
        if self._is_scalar:
            args = [self, accum]
            cfunc_name = delayed.cfunc_name.format(output_dtype=self.dtype)
        else:
            args = [self, mask, accum]
            cfunc_name = delayed.cfunc_name
        if delayed.op is not None:
            args.append(delayed.op)
        args.extend(delayed.args)
        args.append(desc)
        # Make the GraphBLAS call
        call(cfunc_name, args)
        if self._is_scalar:
            self._is_empty = False

    @property
    def _name_html(self):
        """Treat characters after _ as subscript"""
        split = self.name.split("_", 1)
        if len(split) == 1:
            return self.name
        return f"{split[0]}<sub>{split[1]}</sub>"

    _expect_type = _expect_type
    _expect_op = _expect_op


class BaseExpression:
    output_type = None

    def __init__(
        self,
        method_name,
        cfunc_name,
        args,
        *,
        at=False,
        bt=False,
        op=None,
        dtype=None,
        expr_repr=None,
    ):
        self.method_name = method_name
        self.cfunc_name = cfunc_name
        self.args = args
        self.at = at
        self.bt = bt
        self.op = op
        if expr_repr is None:
            if len(args) == 1:
                expr_repr = "{0.name}.{method_name}({op})"
            elif len(args) == 2:
                expr_repr = "{0.name}.{method_name}({1.name}, op={op})"
            else:  # pragma: no cover
                raise ValueError(f"No default expr_repr for len(args) == {len(args)}")
        self.expr_repr = expr_repr
        if dtype is None:
            self.dtype = op.return_type
        else:
            self.dtype = dtype

    def new(self, *, dtype=None, mask=None, name=None):
        output = self.construct_output(dtype=dtype, name=name)
        if mask is None:
            output.update(self)
        else:
            _check_mask(mask, output)
            output(mask=mask).update(self)
        return output

    def _format_expr(self):
        return self.expr_repr.format(*self.args, method_name=self.method_name, op=self.op)

    def _format_expr_html(self):
        expr_repr = self.expr_repr.replace(".name", "._name_html")
        return expr_repr.format(*self.args, method_name=self.method_name, op=self.op)


class _Pointer:
    def __init__(self, val):
        self.val = val

    @property
    def _carg(self):
        return self.val.gb_obj

    @property
    def name(self):
        name = self.val.name
        if not name:
            name = f"temp_{type(self.val).__name__.lower()}"
        return f"&{name}"
