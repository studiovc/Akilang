from llvmlite import ir, binding
from core.akitypes import (
    AkiType,
    AkiBool,
    AkiFunction,
    AkiObject,
    AkiTypeMgr,
    AkiPointer,
    AkiBaseInt,
    _int,
)

from core.astree import (
    VarTypeNode,
    VarTypeName,
    VarTypeFunc,
    VarTypePtr,
    BinOpComparison,
    Constant,
    IfExpr,
    Name,
    VarList,
    Assignment,
    WithExpr,
    Prototype,
    LLVMNode,
    Argument,
    StarArgument,
    Function,
    ExpressionBlock,
    External,
    Call,
    AccessorExpr,
    ObjectValue,
    ObjectRef
)
from core.error import (
    AkiNameErr,
    AkiTypeErr,
    AkiOpError,
    AkiBaseErr,
    AkiSyntaxErr,
    LocalException,
)
from core.repl import CMD, REP
from typing import Optional


class FuncState:
    """
    Object for storing function state, such as its symbol table,
    and context information such as the decorator stack and 
    `unsafe` states.
    TODO: let's make this into a context manager, maybe
    """

    def __init__(self):

        # Function currently in context.
        self.fn = None
        self.return_value = None

        # Breakpoint stack for function.
        self.breakpoints = []

        # Symbol table for function.
        self.symtab = {}

        # Varargs reference (if any) for function.
        self.varargs = None

        # Allocations builder.
        self.allocator = None


class AkiCodeGen:
    """
    Code generation module for Akilang.
    """

    def __init__(
        self,
        module: Optional[ir.Module] = None,
        typemgr=None,
        module_name: Optional[str] = None,
    ):

        # Create an LLVM module if we aren't passed one.

        if module is None:
            self.module = ir.Module(name=module_name)
            self.module.triple = binding.Target.from_default_triple().triple
        else:
            self.module = module

        self.fn: Optional[FuncState] = None
        self.text: Optional[str] = None
        self.unsafe_set = False

        # Create a type manager module if we aren't passed one.

        if typemgr is None:
            self.typemgr = AkiTypeMgr(module=self.module)
            self.types = self.typemgr.types
        else:
            self.typemgr = typemgr
            self.types = typemgr.types

        # Other codegen modules to check for namespaces.
        # Resolved top to bottom.

        self.other_modules: list = []

        self.const_enum = 0

    def _const_counter(self):
        self.const_enum += 1
        return self.const_enum

    def init_func_handlers(self):
        """
        Called when we create a new function.
        This sets up the state of the function,
        as used elsewhere throughout the module.
        """

        self.builder = None
        self.fn = FuncState()

    def eval(self, ast):
        """
        Evaluates an AST expression tree beginning with a top-level node.
        """

        for _ in ast:
            self._codegen(_)

    def _codegen(self, node):
        """
        Dispatch function for codegen based on AST classes.
        """        
        method = f"_codegen_{node.__class__.__name__}"
        result = getattr(self, method)(node)
        return result

    def _codegen_LLVMNode(self, node):
        return node.llvm_node

    def _name(self, node, name_to_find, other_module=None):
        """
        Retrieve a name reference, not the underlying value,
        from the symbol table or the list of globals.
        """

        # First, look in the function symbol table:
        name = self.fn.symtab.get(name_to_find, None)

        if name is not None:
            return name

        # Next, look in the globals:
        name = self.module.globals.get(name_to_find, None)

        if name is not None:
            return name

        # Next, look in other modules:
        for _ in self.other_modules:
            name = _.module.globals.get(name_to_find, None)
            if name is not None:
                # emit function reference for this module
                link = ir.Function(self.module, name.ftype, name.name)
                # copy aki data for function
                link.akinode = name.akinode
                link.akitype = name.akitype
                for n_arg, l_arg in zip(name.args, link.args):
                    l_arg.akinode = n_arg.akinode
                return name

        if name is None:
            raise AkiNameErr(
                node, self.text, f'Name "{CMD}{name_to_find}{REP}" not found'
            )

    def _alloca(self, node, llvm_type, name, size=None, is_global=False, is_heap = False):
        """
        Allocate space for a variable.
        Right now this is stack-only; eventually it'll include
        heap allocations, too.
        We must also later make distinctions between allocations that are for
        a REFERENCE (e.g., a pointer to a string construction) and for an
        UNDERLYING VALUE (e.g., the heap-allocated string itself). That way
        we can track and dispose those by way of scopes.
        """

        allocation = self.fn.allocator.alloca(llvm_type, size, name)
        return allocation

    def _delete_var(self, name):
        """
        Deletes a variable from the local scope.
        Eventually this will be where we make decisions about
        deallocating heap-allocated objects when they
        pass out of scope, etc.
        """

        del self.fn.symtab[name]

    #################################################################
    # Type AST node walker
    #################################################################

    def _get_vartype(self, node: VarTypeNode):
        """
        Looks up a type in the current module
        based on a `VarType` AST node sequence.
        """
        if node is None:
            print ("OK")
            return self.typemgr._default
            #return self._get_type_by_name(self.typemgr._default.type_id)
        if isinstance(node, AkiType):
            return node
        return getattr(self, f"_get_vartype_{node.__class__.__name__}")(node)

    def _get_vartype_Name(self, node):
        # TODO: this is a shim to get around the fact that we have
        # no syntactical way to distinguish a variable-associated
        # typeref (x:i32) with a bare type ref (cast(x,i32))
        return self._get_vartype_VarTypeName(node)

    def _get_vartype_VarTypeName(self, node):
        """
        Node visitor for `VarTypeName` nodes.
        """
        if node.name is None:
            id_to_get = self.typemgr._default.type_id
        else:
            id_to_get = node.name

        var_lookup = self._get_type_by_name(id_to_get)
        if var_lookup is None:
            raise AkiTypeErr(
                node, self.text, f'Unrecognized type definition "{CMD}{id_to_get}{REP}"'
            )

        return var_lookup

    def _get_vartype_VarTypeAccessor(self, node):                 
        base_type = self._get_vartype(node.vartype)
        accessors = []
        for _ in node.accessors.accessors:
            accessor_type = self._get_vartype(_.vartype)
            if not isinstance(accessor_type, AkiBaseInt):
                raise AkiSyntaxErr(
                    _, self.text,
                    "Array indices must be integer constants"
                )
            accessors.append([_.val,accessor_type])            
        array_type = self.types['array'].new(self, node, base_type, accessors)
        return array_type


    def _get_vartype_VarTypePtr(self, node):
        """
        Node visitor for `VarTypePtr` nodes.
        """
        aki_type = self._get_vartype(node.pointee)
        aki_type = self.typemgr.as_ptr(aki_type, literal_ptr=True)
        return aki_type

    def _get_vartype_VarTypeFunc(self, node):
        """
        Node visitor for `VarTypeFunc` nodes.
        """
        for _ in node.arguments:
            _.akitype = self._get_vartype(_)
            _.llvm_type = _.akitype.llvm_type

        node.return_type.akitype = self._get_vartype(node.return_type)

        aki_node = AkiFunction(node.arguments, node.return_type.akitype)
        node.name = aki_node.type_id

        return aki_node

    def _get_type_by_name(self, type_name):
        """
        Find a type in the current module by name.
        Does not distinguish between built-in types and
        types registered with the module, e.g., by function signatures.
        """
        type_to_find = self.types.get(type_name, None)
        if type_to_find is None:
            return None
        if isinstance(type_to_find, AkiType):
            return type_to_find
        return None

    #################################################################
    # Utilities
    #################################################################

    def _move_before_terminator(self, block):
        """
        Position in an existing LLVM block before the terminator instruction.
        """
        assert block.is_terminal
        self.builder.position_before(block.instructions[-1])

    def _check_var_name(self, node, name, is_global=False):
        """
        Check routine to determine if a given name is already in use
        in a given context.
        """
        context = self.module.globals if is_global else self.fn.symtab
        if name in context:
            raise AkiNameErr(
                node, self.text, f'Name "{CMD}{name}{REP}" already used in this context'
            )

    def _scalar_as_bool(self, node, expr):
        """
        Takes an LLVM instruction result of a scalar type
        and converts it to a boolean type.
        """
        result = self._codegen(
            BinOpComparison(
                node,
                "!=",
                LLVMNode(node, expr.akitype, expr),
                Constant(node, expr.akitype.default(self, node), expr.akitype),
            )
        )
        return result

    def _is_type(self, node, expr, other_type):
        akitype = getattr(expr, "akitype", None)
        if not akitype:
            raise AkiSyntaxErr(node, self.text, f"Expression does not yield a value")
        return isinstance(akitype, other_type)

    def _type_check_op(self, node, lhs, rhs):
        """
        Perform a type compatibility check for a binary op.
        This takes in two LLVM nodes decorated with Aki data.
        """

        self._is_type(node.lhs, lhs, AkiType)
        lhs_atype = lhs.akitype

        self._is_type(node.rhs, rhs, AkiType)
        rhs_atype = rhs.akitype

        if lhs_atype != rhs_atype:

            error = f'"{CMD}{lhs.akinode.name}{REP}" ({CMD}{lhs_atype}{REP}) and "{CMD}{rhs.akinode.name}{REP}" ({CMD}{rhs_atype}{REP}) do not have compatible types for operation "{CMD}{node.op}{REP}"'

            if lhs_atype.signed != rhs_atype.signed:
                is_signed = lambda x: "Signed" if x else "Unsigned"
                error += f'\nTypes also have signed/unsigned disagreement:\n - "{CMD}{lhs.akinode.name}{REP}" ({CMD}{lhs_atype}{REP}): {is_signed(lhs_atype.signed)}\n - "{CMD}{rhs.akinode.name}{REP}" ({CMD}{rhs_atype}{REP}): {is_signed(rhs_atype.signed)}'

            raise AkiTypeErr(node, self.text, error)
        return lhs_atype, rhs_atype

    #################################################################
    # Top-level statements
    #################################################################

    def _codegen_Prototype(self, node):
        """
        Generate a function prototype for the LLVM module
        from the `Prototype` AST node.
        """
        # Name collision check

        self._check_var_name(node, node.name, True)

        # Generate function arguments.
        # Add LLVM type information to each argument,
        # based on the type information available in the node.

        func_args = []
        node_args = []

        require_defaults = False

        # Node.arguments are Aki type nodes

        for _ in node.arguments:

            # if this is a starred argument,
            # then we activate the flag that says this function
            # receives varargs
            # but we don't add this to the func_args list
            # we add the vararg name to a context variable for the function
            # so we can look up the call results
            # look into how this works in LLVM
            # the lookup is done at runtime
            # eventually starargs will be a simple list or tuple object
            # but for externs we can encode it this way

            # when we parse the call,
            # once we hit the starred argument in the argument list,
            # then we just accept whatever submitted arguments there are
            # and we make them available in a special variable
            # that is stored with the function

            if isinstance(_, StarArgument):
                self.fn.varargs = _
                continue

            if _.default_value is not None:
                require_defaults = True
                if isinstance(_.default_value, ExpressionBlock):
                    raise AkiSyntaxErr(
                        _.default_value,
                        self.text,
                        f"Function argument defaults cannot be an expression block (yet)",
                    )

            if not _.default_value and require_defaults:
                raise AkiSyntaxErr(
                    _,
                    self.text,
                    f'Function "{node.name}" has non-default argument "{_.name}" after default arguments',
                )
            if _.vartype is None:
                #_.vartype.name = self.typemgr._default.type_id
                _.vartype = self.typemgr._default
            arg_vartype = self._get_vartype(_.vartype)

            _.vartype.llvm_type = arg_vartype.llvm_type
            _.vartype.akitype = arg_vartype

            # The func_args supplied to the f_type call
            # are standard LLVM types
            func_args.append(arg_vartype.llvm_type)
            node_args.append(_)

        # Set return type.
        if node.return_type is None:
            node.return_type = VarTypeName(node, self.typemgr._default.type_id)
            
        return_type = self._get_vartype(node.return_type)
        node.return_type.akitype = return_type

        # This is for the sake of compatibility with
        # things that expect a `vartype`
        node.vartype = node.return_type

        # Generate function prototype.

        f_type = ir.FunctionType(
            return_type.llvm_type, func_args, var_arg=self.fn.varargs is not None
        )
        f_type.return_type.akitype = return_type

        for p_arg, n_arg in zip(f_type.args, node.arguments):
            p_arg.akinode = n_arg

        proto = ir.Function(self.module, f_type, name=node.name)

        proto.calling_convention = "fastcc" if self.fn.varargs is None else "ccc"

        # Set variable types for function

        function_type = AkiFunction([_.vartype for _ in node_args], return_type)

        proto.akinode = node
        proto.akitype = function_type

        # Add Aki type metadata
        # TODO:
        # store the original string for the function sig and use that

        # aki_type_metadata = self.module.add_metadata([str(proto.akitype)])
        # proto.set_metadata("aki.type", aki_type_metadata)

        return proto

    def _codegen_External(self, node):
        """
        Generate an external function call from an `External` AST node.
        """

        return self._codegen_Function(node)

    def _codegen_Function(self, node):
        """
        Generate an LLVM function from a `Function` AST node.
        """

        self.init_func_handlers()

        # Generate function prototype.
        func = self._codegen(node.prototype)

        if self.fn.varargs and not isinstance(node, External):
            raise AkiSyntaxErr(
                self.fn.varargs.p,
                self.text,
                f"Non-external functions don't yet support variable arguments",
            )

        # Store an original function reference in the prototype.
        # This is so we can refer to it later if we use
        # a function pointer.
        func.akitype.original_function = func

        self.fn.fn = func

        if isinstance(node, External):
            for a, b in zip(func.args, node.prototype.arguments):
                # make sure the variable name is not in use
                self._check_var_name(b, b.name)
                # set its Aki attributes
                # var_alloc.akitype = b.vartype.akitype
                # var_alloc.akinode = b
                # set the akinode attribute for the original argument,
                # so it can be referenced if we need to throw an error
                a.akitype = b.vartype.akitype
                a.akinode = b
            return func

        # Generate entry block and function body.

        self.entry_block = func.append_basic_block("entry")
        #self.fn.alloc_block = self.entry_block
        self.fn.allocator = ir.IRBuilder(self.entry_block)

        # Add prototype arguments to function symbol table
        # and add references in function.
        # Use isinstance(ir.Argument) to determine if the
        # var being looked up is a func arg.

        for a, b in zip(func.args, node.prototype.arguments):
            # make sure the variable name is not in use
            self._check_var_name(b, b.name)
            # create an allocation for the variable
            var_alloc = self._alloca(b, b.vartype.llvm_type, b.name)
            # set its Aki attributes
            a.akitype = b.vartype.akitype
            var_alloc.akitype = b.vartype.akitype
            var_alloc.akinode = b
            # set the akinode attribute for the original argument,
            # so it can be referenced if we need to throw an error
            a.akinode = b
            # add the variable to the symbol table
            self.fn.symtab[b.name] = var_alloc
            # store the default value to the variable
            self.fn.allocator.store(a, var_alloc)

        # Add return value holder.

        self.fn.return_value = self._alloca(
            node, func.return_value.type, ".function_return_value"
        )

        # Set Akitype values for the return value holder
        # and for the function's actual return value.

        self.fn.return_value.akitype = func.akitype.return_type
        func.return_value.akitype = func.akitype.return_type

        # Create actual starting function block and codegen instructions.

        self.body_block = func.append_basic_block("body")
        self.builder = ir.IRBuilder(self.body_block)
        self.builder.position_at_start(self.body_block)

        result = self._codegen(node.body)

        # If we have an empty function body,
        # load the default value for the return type
        # and return that.

        assert isinstance(result, ir.Instruction)

        # TODO: We need to set a flag somewhere indicating that the function
        # did not return a value, so that it can be used later.
        # (for instance, when checking to see if a given statement returns a value)
        # This could be set in the LLVM function object,
        # in the Aki type for the function definition,
        # or in the AST node. Not sure which one would be best.

        if result is None:
            result = self._codegen(
                Constant(
                    node.body.p,
                    node.prototype.return_type.akitype.default(self, node),
                    node.prototype.return_type,
                )
            )

        # If we don't explicitly assign a return type on the function prototype,
        # we infer it from the return value of the body.

        if node.prototype.return_type.name is None:
            r_type = result.akitype

            # Set the result holder
            self.fn.return_value.type = r_type.llvm_type.as_pointer()
            self.fn.return_value.akitype = r_type

            # Set the actual type for the function return value
            func.return_value = r_type.llvm_type

            # Set the return value type as used by the REPL
            func.return_value.akitype = r_type

            # Set the return type on the function's own signature
            # func.type.pointee.return_type = r_type.llvm_type
            func.ftype.return_type = r_type.llvm_type

            # Set the Aki type node for the function
            func.akitype.return_type = r_type

        # If the function prototype and return type still don't agree,
        # throw an exception

        if result.akitype != self.fn.return_value.akitype:
            raise AkiTypeErr(
                node,
                self.text,
                f'Return value from function "{CMD}{func.name}{REP}" ({CMD}{result.akitype}{REP}) does not match function signature return type ({CMD}{self.fn.return_value.akitype}{REP})',
            )

        # Add return value for function in exit block.

        self.builder.store(result, self.fn.return_value)

        # branch to exit, return the return value.

        exit_block = func.append_basic_block("exit")
        self.builder.branch(exit_block)

        self.builder.position_at_start(exit_block)
        self.builder.ret(self.builder.load(self.fn.return_value, ".ret"))

        self.fn.allocator.branch(self.body_block)

        # Reset function state handlers.

        self.fn = None

        # Add the function signature to the list of types for the module,
        # using the function's name
        # We have to do this here because the signature may have mutated
        # during the construction process.

        _ = self.typemgr.add_type(node.prototype.name, func.akitype, self.module)
        if _ is None:
            raise AkiTypeErr(node, self.text, "Invalid name")
        func.enum_id = func.akitype.enum_id

        return func

    #################################################################
    # Blocks
    #################################################################

    def _codegen_ExpressionBlock(self, node):
        """
        Codegen each expression in an `Expression` Block.
        """
        result = None
        for _ in node.body:
            result = self._codegen(_)
        return result

    def _codegen_UnsafeBlock(self, node):
        self.unsafe_set = True
        return self._codegen(node.expr_block)
        self.unsafe_set = False

    #################################################################
    # Declarations
    #################################################################

    def _codegen_VarList(self, node):
        """
        Codegen the variables in a `VarList` node.
        """
        for _ in node.vars:

            self._check_var_name(_, _.name)

            # Create defaults if no value or vartype

            # If no value ...

            value = None

            if _.val is None:

                # and no default vartype, then create the default

                if _.vartype is None:
                    #_.vartype = Name(_.p, self.types.default.type_id)
                    _.vartype = Name(_.p, self.typemgr._default.type_id)

                _.akitype = self._get_vartype(_.vartype)
                _.val = Constant(_.p, _.akitype.default(self, node), _.vartype)
                value = _.val

            else:

                # If there is a value ...

                value = self._codegen(_.val)

                # and there is no type identifier on the variable ...

                # FIXME: do NOT rely on the name!
                # if we have no vartype, it should just be None

                if _.vartype is None:
                    # then use the value's variable type
                    _.vartype = value.akinode.vartype
                    _.akitype = value.akitype
                else:
                    _.akitype = self._get_vartype(_.vartype)

                value = LLVMNode(_.val, _.vartype, value)

            # Create an allocation for that type
            var_ptr = self._alloca(_, _.akitype.llvm_type, _.name)

            # Store its node attributes
            var_ptr.akitype = _.akitype
            var_ptr.akinode = _

            # Store the variable in the function symbol table
            self.fn.symtab[_.name] = var_ptr

            # and store the value itself to the variable
            # by way of an Assignment op
            self._codegen(Assignment(_.p, "=", 
                ObjectRef(_.p, Name(_.p, _.name)), value))

    #################################################################
    # Control flow
    #################################################################

    def _codegen_Call(self, node):
        """
        Generate a function call from a `Call` node.
        """

        # check if this is a builtin

        builtin = getattr(self, f"_builtins_{node.name}", None)
        if builtin:
            return builtin(node)

        # TODO: get a pre-generated AST node that represents the builtin,
        # verify its arguments and other behavior as we would another function call,
        # then instead of codegenning a call, we codegen the body inline

        # check if this is a request for a type
        # this will eventually go somewhere else

        try:

            named_type = self._get_type_by_name(node.name)

            if named_type is not None and not isinstance(named_type, AkiFunction):

                if len(node.arguments) != 1:

                    # Create a fake function definition to handle the error

                    call_func = self._codegen(
                        Function(
                            node,
                            Prototype(
                                node,
                                node.name,
                                [
                                    Argument(
                                        node,
                                        "vartype",
                                        VarTypeName(node, self.types["type"].type_id),
                                    )
                                ],
                                VarTypeName(node, self.types["type"].type_id),
                            ),
                            ExpressionBlock(node, []),
                        )
                    )

                    raise LocalException

                arg = node.arguments[0]

                # this will eventually become a builtin
                if node.name == "type":
                    type_from = self._codegen(arg)
                    const = self._codegen(
                        Constant(arg, type_from.akitype.enum_id, self.types["type"])
                    )
                    return const

                # this will also eventually become a builtin

                if isinstance(arg, Constant):
                    # this check is in place until we have
                    # methods for making ints from floats, etc.
                    if arg.vartype.name != named_type.type_id:
                        raise AkiTypeErr(
                            arg,
                            self.text,
                            f'Constant "{CMD}{arg.val}{REP}" is not type "{CMD}{named_type.type_id}{REP}" (type conversions not yet performed this way)',
                        )

                    const = self._codegen(
                        Constant(arg, arg.val, VarTypeName(arg, named_type.type_id))
                    )
                    return const

                else:
                    raise AkiOpError(
                        node.arguments[0], self.text, f"Only constants allowed for now"
                    )

            call_func = self._name(node, node.name)
            args = []

            # If this is a function pointer, get the original function

            if isinstance(call_func, ir.AllocaInstr):
                cf = call_func.akitype.original_function
                if cf is None:
                    raise AkiTypeErr(
                        node,
                        self.text,
                        f'"{CMD}{node.name}{REP}" is "{CMD}{call_func.akitype}{REP}", not a function',
                    )
                else:
                    call_func = cf

            # If we have too many arguments,
            # and we're not processing a vararg function, give up

            if (
                len(node.arguments) > len(call_func.args)
                and not call_func.ftype.var_arg
            ):
                raise LocalException

            total_args = max(len(node.arguments), len(call_func.args))
            for _ in range(total_args):

                # if we're supplying more arguments than are available
                # to the called function,
                # see if this is a vararg

                if _ > len(call_func.args) - 1:
                    if call_func.ftype.var_arg:
                        arg = node.arguments[_]
                        arg_val = self._codegen(arg)
                        args.append(arg_val)
                        continue
                    else:
                        raise LocalException

                f_arg = call_func.args[_]

                # If we're out of supplied arguments,
                # see if the function has default args.

                if _ > len(node.arguments) - 1:
                    default_arg_value = f_arg.akinode.default_value
                    if default_arg_value is None:
                        raise LocalException
                    arg_val = self._codegen(default_arg_value)
                    args.append(arg_val)
                    continue

                # If we still have supplied arguments,
                # use them instead.

                arg = node.arguments[_]
                arg_val = self._codegen(arg)

                if arg_val.type != call_func.args[_].type:
                    raise AkiTypeErr(
                        arg,
                        self.text,
                        f'Value "{CMD}{arg.name}{REP}" of type "{CMD}{arg_val.akitype}{REP}" does not match {CMD}{node.name}{REP} argument {CMD}{_+1}{REP} of type "{CMD}{call_func.args[_].akinode.vartype.akitype}{REP}"',
                    )

                args.append(arg_val)

        # TODO: list in error which arguments are optional, along with their defaults

        except LocalException:
            args = "\n".join(
                [
                    f"arg {index+1} = {CMD}{_.name}{_.vartype.akitype}{REP}"
                    for index, _ in enumerate(call_func.akinode.arguments)
                ]
            )
            raise AkiSyntaxErr(
                node,
                self.text,
                f'Function call to "{CMD}{node.name}{REP}" expected {CMD}{len(call_func.args)}{REP} arguments but got {CMD}{len(node.arguments)}{REP}\n{args}',
            )

        call = self.builder.call(call_func, args, call_func.name + ".call")
        call.akitype = call_func.akitype.return_type
        call.akinode = call_func.akinode
        return call

    def _codegen_Break(self, node):
        """
        Codegen a `break` action.
        """

        if not self.fn.breakpoints:
            raise AkiSyntaxErr(
                node, self.text, f'"break" not called within a loop block'
            )

        self.builder.branch(self.fn.breakpoints[-1])

    #################################################################
    # Expressions
    #################################################################

    def _codegen_WithExpr(self, node):
        """
        Codegen a `with` block.
        """

        self._codegen(node.varlist)
        body = self._codegen(node.body)
        for _ in node.varlist.vars:
            self._delete_var(_.name)
        return body

    def _codegen_LoopExpr(self, node):
        """
        Codegen a `loop` expression.
        """

        local_symtab = {}

        # If there are no elements in the loop declaration,
        # assume an infinite loop

        if node.conditions == []:

            start = None
            stop = None
            step = None

        else:

            # Create the loop initialization block

            # TODO:
            # If we only have one element, assume it's the start.
            # If only two, start/stop.
            # If three, start/stop/step.

            if len(node.conditions) != 3:
                raise AkiSyntaxErr(
                    node,
                    self.text,
                    f'"loop" must have three elements (start, stop, step)',
                )

            start = node.conditions[0]
            stop = node.conditions[1]
            step = node.conditions[2]

            loop_init = self.builder.append_basic_block("loop_init")
            self.builder.branch(loop_init)
            self.builder.position_at_start(loop_init)

            # if the first element is a varlist,
            # instantiate each variable in the symbol table,
            # and keep a copy for ourselves so we can
            # delete it later.

            if isinstance(start, VarList):
                self._codegen(start)
                for _ in start.vars:
                    local_symtab[_.name] = self.fn.symtab[_.name]

            # If the first element is just an assignment node,
            # then codegen assignments to the function symbol table.

            elif isinstance(start, Assignment):
                self._codegen(start)

            else:
                raise AkiSyntaxErr(
                    start,
                    self.text,
                    f'"loop" element 1 must be a variable declaration or variable assignment',
                )

        if stop:

            loop_test = self.builder.append_basic_block("loop_test")
            self.builder.branch(loop_test)
            self.builder.position_at_start(loop_test)
            loop_condition = self._codegen(stop)
            with self.builder.if_else(loop_condition) as (then_clause, else_clause):
                with then_clause:
                    loop_body = self._codegen(node.body)
                    n = self._codegen(Assignment(step, "+", 
                    ObjectRef(step, step.lhs), step))
                    self.builder.branch(loop_test)
                with else_clause:
                    pass

        else:

            loop = self.builder.append_basic_block("loop_inf")
            loop_exit = self.builder.append_basic_block("loop_exit")
            self.fn.breakpoints.append(loop_exit)
            self.builder.branch(loop)
            self.builder.position_at_start(loop)
            loop_body = self._codegen(node.body)
            self.builder.branch(loop)
            self.builder.position_at_start(loop_exit)
            self.fn.breakpoints.pop()

        # Remove local objects from symbol table

        for _ in local_symtab:
            self._delete_var(_)

        return loop_body

    def _codegen_IfExpr(self, node, is_when_expr=False):
        """
        Codegen an `if` or `when` expression, where then and else return values are of the same type. The `then/else` nodes are raw AST nodes.
        Because the expressions could be indeterminate, we have to codegen them
        to get a vartype.
        """

        if_expr = self._codegen(node.if_expr)

        if not self._is_type(node.if_expr, if_expr, AkiBool):
            if_expr = self._scalar_as_bool(node.if_expr, if_expr)

        if_block = self.builder._block

        # codegen the clauses so we can determine their return types

        with self.builder.if_else(if_expr) as (then_clause, else_clause):
            with then_clause:
                then_block = self.builder._block
                then_result = self._codegen(node.then_expr)
            with else_clause:
                else_block = self.builder._block
                if node.else_expr:
                    else_result = self._codegen(node.else_expr)

        exit_block = self.builder._block

        # for if expresssion, typematch results

        if not is_when_expr:

            if then_result.akitype != else_result.akitype:
                raise AkiTypeErr(
                    node.then_expr,
                    self.text,
                    f'"{CMD}if/else{REP}" must yield same type; use "{CMD}when/else{REP}" for results of different types',
                )

            self._move_before_terminator(if_block)

            if_result = self._alloca(
                node.then_expr, then_result.akitype.llvm_type, ".if_result"
            )

            self._move_before_terminator(then_block)
            self.builder.store(then_result, if_result)

            self._move_before_terminator(else_block)
            self.builder.store(else_result, if_result)

            self.builder.position_at_start(exit_block)
            result = if_result
            result_akitype = then_result.akitype

        # for when expressions, just return the if clause value

        else:

            self._move_before_terminator(if_block)

            if_result = self._alloca(
                node.if_expr, if_expr.akitype.llvm_type, ".when_result"
            )
            self.builder.position_at_start(exit_block)

            result = self.builder.store(if_expr, if_result)
            result_akitype = if_expr.akitype

        result = self.builder.load(if_result)
        result.akitype = result_akitype
        result.akinode = node
        result.akinode.name = f'"if" expr'
        return result

    def _codegen_WhenExpr(self, node):
        """
        Codegen a `when` expression, which returns the value of the `when` itself.
        """
        return self._codegen_IfExpr(node, True)

    #################################################################
    # Operations (also expressions)
    #################################################################

    def _codegen_UnOp(self, node):
        """
        Generate a unary op from an AST `UnOp` node.
        """
        op = self._unops.get(node.op, None)

        if op is None:
            raise AkiOpError(
                node, self.text, f'Operator "{CMD}{node.op}{REP}" not supported'
            )

        operand = self._codegen(node.lhs)
        instr = op(self, node, operand)
        instr.akiype = operand.akitype
        instr.akinode = node
        instr.akinode.name = f'op "{node.op}"'
        return instr

    def _codegen_UnOp_Neg(self, node, operand):
        """
        Generate a unary negation operation for a scalar value.
        """

        op = getattr(operand.akitype, "unop_neg", None)
        if op is None:
            raise AkiOpError(
                node,
                self.text,
                f'Operator "{CMD}{node.op}{REP}" not supported for type "{CMD}{operand.akitype}{REP}"',
            )

        instr = op(self, node, operand)
        instr.akitype = operand.akitype
        instr.akinode = node
        instr.akinode.name = f'op "{node.op}"'
        return instr

    def _codegen_UnOp_Not(self, node, operand):
        """
        Generate a NOT operation for a true/false value.
        """
        # if not isinstance(operand.akitype, AkiBool):
        if not self._is_type(node, operand, AkiBool):
            operand = self._scalar_as_bool(node, operand)

        xor = self.builder.xor(
            operand, self._codegen(Constant(node, 1, operand.akitype))
        )

        xor.akitype = self.types["bool"]
        xor.akinode = node
        xor.akinode.name = f'op "{node.op}"'
        return xor

    _unops = {"-": _codegen_UnOp_Neg, "not": _codegen_UnOp_Not}

    def _codegen_BinOpComparison(self, node):
        """
        Generate a comparison instruction (boolean result) for an op.
        """

        lhs = self._codegen(node.lhs)
        rhs = self._codegen(node.rhs)


        # Type checking for operation
        lhs_atype, rhs_atype = self._type_check_op(node, lhs, rhs)
        signed_op = lhs_atype.signed

        # Find and add appropriate instruction

        try:
            instr_name = lhs_atype.comp_ins
            if instr_name is None:
                raise LocalException
            instr_type = getattr(self.builder, instr_name)
            op_name = lhs_atype.comp_ops.get(node.op, None)
            if op_name is None:
                raise LocalException

        except LocalException:
            raise AkiOpError(
                node,
                self.text,
                f'Comparison operator "{CMD}{node.op}{REP}" not supported for type "{CMD}{lhs_atype}{REP}"',
            )

        instr = instr_type(node.op, lhs, rhs, op_name)

        instr.akitype = self.types["bool"]
        instr.akinode = node
        instr.akinode.name = f'op "{node.op}"'

        return instr

    def _codegen_BinOp(self, node):
        """
        Codegen a generic binary operation, typically math.
        """
        lhs = self._codegen(node.lhs)
        rhs = self._codegen(node.rhs)

        # Type checking for operation
        lhs_atype, rhs_atype = self._type_check_op(node, lhs, rhs)
        signed_op = lhs_atype.signed

        # Generate instructions for a binop that yields
        # a value of the same type as the inputs.
        # Use bin_ops property of the Aki type class.

        try:
            instr_type = lhs_atype
            op_types = getattr(lhs_atype, "bin_ops", None)
            if op_types is None:
                raise LocalException
            math_op = op_types.get(node.op, None)
            if math_op is None:
                raise LocalException
            instr_call = getattr(lhs_atype.__class__, f"binop_{math_op}")
            instr = instr_call(lhs_atype, self, node, lhs, rhs, node.op)
            # (Later for custom types we'll try to generate a call)

        except LocalException:
            raise AkiOpError(
                node,
                self.text,
                f'Binary operator "{CMD}{node.op}{REP}" not found for type "{CMD}{lhs_atype}{REP}"',
            )

        instr.akitype = instr_type
        instr.akinode = node
        instr.akinode.name = f'op "{node.op}"'
        return instr

        # TODO: This assumes the left-hand side will always have the correct
        # type information to be propagated. Need to confirm this.

    def _codegen_AccessorExpr(self, node, load=True):
        # XXX: this should be a direct extraction codegen
        expr = self._name(node.expr, node.expr.name)
        index = getattr(expr.akitype, 'op_index', None)
        if index is None:
            raise AkiTypeErr(
                node.expr, self.text,
                "No index operator found for this type"
            )
        result = index(self, node, expr)
        if load:
            t= result.akitype
            result = self.builder.load(result)
            result.akitype=t
        return result

    #################################################################
    # Values
    #################################################################

    def _codegen_ObjectRef(self, node):
        # TODO: direct codegen of the node that does noload
        if isinstance(node.expr, Name):
            return self._name(node.expr, node.expr.name)
        if isinstance(node.expr,AccessorExpr):
            return self._codegen_AccessorExpr(node.expr, False)
        raise AkiOpError(
                node,
                self.text,
                f'Assignment target "{CMD}{node.lhs}{REP}" must be a variable',
            )

    def _codegen_ObjectValue(self, node):
        pass
    
    def _codegen_Assignment(self, node):
        """
        Assign value to variable pointer.        
        """

        # `lhs` should be an ObjectRef node.
        # This `assert` will go away once we establish all
        # the code paths to an Assignment.

        assert isinstance(node.lhs, ObjectRef)

        lhs = node.lhs
        rhs = node.rhs

        ptr = self._codegen(lhs)
        val = self._codegen(rhs)

        self._type_check_op(node, ptr, val)
        self.builder.store(val, ptr)

        return val

    def _codegen_Name(self, node):
        """
        Generate a variable reference from a name.
        This always assumes we want the variable value associated with the name,
        not the variable's pointer.
        For that, use ObjectRef.
        This will eventually no longer be its own node, I think
        """

        # Types are returned, for now, as their enum
        # TODO: Where possible in the grammar, generate nodes that return these directly.

        named_type = self._get_type_by_name(node.name)

        # TODO: if this is a function, should we look up the registered type?
        if named_type is not None and not isinstance(named_type, AkiFunction):
            return self._codegen(Constant(node, named_type.enum_id, self.types["type"]))

        name = self._name(node, node.name)

        # Return object types as a pointer

        if self._is_type(node, name, AkiObject):
            # ... by way of a variable (ir.AllocaInstr)
            if isinstance(name, ir.AllocaInstr):
                # then load that from the pointer
                load = self.builder.load(name)
                load.akinode = name.akinode
                load.akitype = name.akitype
                return load
            # otherwise just return the object
            return name

        # Otherwise, load and decorate the value
        load = self.builder.load(name)
        load.akinode = name.akinode
        load.akitype = name.akitype
        return load

    def _codegen_Constant(self, node):
        """
        Generate an LLVM `ir.Constant` value from a `Constant` AST node.
        """

        # Get the Aki variable type for the node
        vartype = self._get_vartype(node.vartype)

        # Create an LLVM constant using the derived vartype
        constant = ir.Constant(vartype.llvm_type, node.val)

        # Set the name of the Aki node to the constant value
        # (we use this in error messages, etc.)
        node.name = node.val

        # Set the Aki type for the node to the derived vartype
        node.akitype = vartype

        # Set the LLVM constant's own Aki properties
        constant.akinode = node
        constant.akitype = vartype

        return constant

    def _codegen_String(self, node):
        """
        Generates a *compile-time* string constant.
        """

        const_counter = self._const_counter()

        akitype = self._get_vartype(node.vartype)
        data, data_array = self.types["str"].data(node.val)

        # TODO: I'm considering moving this into .data
        # to keep this module leaner
        # we may also need access to it there so that we can
        # generate empty strings

        string = ir.GlobalVariable(
            self.module, data_array, f".str.data.{const_counter}"
        )
        string.initializer = ir.Constant(data_array, data)

        string.global_constant = True
        string.unnamed_addr = True

        data_object = ir.GlobalVariable(
            self.module, self.types["str"].llvm_type_base, f".str.{const_counter}"
        )

        data_object.initializer = ir.Constant(
            self.types["str"].llvm_type_base,
            (
                (self.types["str"].enum_id, len(data_array), 0, 0),
                string.bitcast(self.typemgr.as_ptr(self.types["u_mem"]).llvm_type),
            ),
        )

        data_object.akitype = akitype
        data_object.akinode = node
        return data_object

    #################################################################
    # Builtins
    #################################################################

    # Builtins are not reserved words, but functions that cannot be
    # expressed by way of other Aki code due to low-level manipulations.
    # This list should remain as small as possible.

    # TODO: Create function signatures for these so we can auto-check
    # argument counts, types, etc.

    def _builtins_cast(self, node):
        if not self.unsafe_set:
            raise AkiSyntaxErr(
                node,
                self.text,
                f'"{CMD}cast{REP}" requires an "{CMD}unsafe{REP}" block',
            )
        if len(node.arguments) != 2:
            raise AkiSyntaxErr(
                node, self.text, f'"{CMD}cast{REP}" requires 2 arguments'
            )
        node_ref = node.arguments[0]
        target_type = node.arguments[1]

        c1 = self._codegen(node_ref)
        c2 = self._get_vartype(target_type)

        # if isinstance(target_type, Name):
        #     target_type = VarTypeName(target_type.p, target_type.name)

        target_data = self.typemgr.target_data()
        c1_size = c1.type.get_abi_size(target_data)
        c2_size = c2.llvm_type.get_abi_size(target_data)

        try:

            # object casts are not OK

            if isinstance(c1.akitype, AkiObject):
                raise AkiTypeErr(
                    node_ref,
                    self.text,
                    f"Objects are not a valid source for {CMD}cast{REP}",
                )

            if isinstance(c2, AkiObject):
                raise AkiTypeErr(
                    target_type,
                    self.text,
                    f"Objects are not a valid target for {CMD}cast{REP}",
                )

            # same size, so pointer cast is OK

            if c1_size == c2_size:
                if isinstance(c1.akitype, AkiBaseInt) and isinstance(c2, AkiPointer):
                    c3 = self.builder.inttoptr(c1, c2.llvm_type)
                    raise LocalException
                elif isinstance(c1.akitype, AkiPointer) and isinstance(c2, AkiBaseInt):
                    c3 = self.builder.ptrtoint(c1, c2.llvm_type)
                    raise LocalException
                else:
                    c3 = self.builder.bitcast(c1, c2.llvm_type)
                    raise LocalException

            # different size, pointer cast not OK

            if isinstance(c1.akitype, AkiPointer) or isinstance(c2, AkiPointer):
                raise AkiTypeErr(
                    node_ref,
                    self.text,
                    f"Types must be of same size for pointer {CMD}cast{REP}",
                )

            # different size, zero-extend or truncate as needed

            if c2_size > c1_size:
                c3 = self.builder.zext(c1, c2.llvm_type)
                raise LocalException
            elif c2_size < c1_size:
                c3 = self.builder.trunc(c1, c2.llvm_type)
                raise LocalException

        except LocalException:
            pass

        c3.akitype = c2
        c3.akinode = node
        c3.akinode.vartype = c2
        return c3

    def _builtins_c_size(self, node):
        if len(node.arguments) != 1:
            raise AkiSyntaxErr(
                node, self.text, f'"{CMD}c_size{REP}" requires 1 argument'
            )
        node_ref = node.arguments[0]
        c1 = self._codegen(node_ref)
        c2 = c1.akitype.c_size(self, c1)
        return c2

    def _builtins_c_data(self, node):
        if len(node.arguments) != 1:
            raise AkiSyntaxErr(
                node, self.text, f'"{CMD}c_data{REP}" requires 1 argument'
            )
        node_ref = node.arguments[0]
        c1 = self._codegen(node_ref)
        c2 = c1.akitype.c_data(self, c1)
        return c2

    def _builtins_ref(self, node):
        if len(node.arguments) != 1:
            raise AkiSyntaxErr(node, self.text, f'"{CMD}ref{REP}" requires 1 argument')
        node_ref = node.arguments[0]

        if not isinstance(node_ref, Name):
            n1 = self._codegen(node_ref)
            raise AkiTypeErr(
                node_ref,
                self.text,
                f'Can\'t derive a reference as "{CMD}{n1.akinode.name}{REP}" is not a variable',
            )
        ref = self._name(node, node_ref.name)

        # if isinstance(ref.akitype, AkiFunction):
        if self._is_type(node_ref, ref, AkiFunction):
            # Function pointers are a special case, at least for now
            r1 = self._codegen(node_ref)
            r2 = self._alloca(node_ref, r1.type, f".{node_ref.name}.ref")
            self.builder.store(r1, r2)
            r2.akinode = node_ref
            r2.akitype = self.typemgr.as_ptr(r1.akitype)
            r2.akitype.llvm_type.pointee.akitype = r1.akitype
            # TODO: This should be created when we allocate the pointer, IMO
            r2.akitype.llvm_type.pointee.akitype.original_function = (
                r1.akitype.original_function
            )
            return r2

        # The `gep` creates a no-op copy of the original value so we can
        # modify its Aki properties independently. Otherwise the original
        # Aki variable reference has its properties clobbered.

        r1 = self.builder.gep(ref, [_int(0)])
        r1.akinode = node_ref
        r1.akitype = self.typemgr.as_ptr(ref.akitype, literal_ptr=True)
        r1.akitype.llvm_type.pointee.akitype = ref.akitype

        return r1

    def _builtins_deref(self, node):
        if len(node.arguments) != 1:
            raise AkiSyntaxErr(
                node, self.text, f'"{CMD}deref{REP}" requires 1 argument'
            )

        node_deref = node.arguments[0]

        if not isinstance(node_deref, Name):
            n1 = self._codegen(node_deref)
            raise AkiTypeErr(
                node_deref,
                self.text,
                f'Can\'t extract a reference as "{CMD}{n1.akinode.name}{REP}" is not a variable',
            )

        ref = self._name(node, node_deref.name)

        # if not isinstance(ref.akitype, AkiPointer):
        if not self._is_type(node, ref, AkiPointer):
            raise AkiTypeErr(
                node_deref,
                self.text,
                f'Can\'t extract a reference as "{CMD}{node_deref.name}{REP}" is not a pointer',
            )

        f0 = self.builder.load(ref)
        f1 = self.builder.load(f0)

        f1.akinode = node_deref
        f1.akitype = ref.akitype.llvm_type.pointee.akitype
        return f1
