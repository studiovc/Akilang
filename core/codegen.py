import warnings
import llvmlite.ir as ir
import llvmlite.binding as llvm

from core.ast_module import (
    Binary, Variable, Prototype, Function, Uni, Class,
    Array, If, Number, ArrayAccessor, Call, Var,
    _ANONYMOUS, Const
)
from core.vartypes import SignedInt, DEFAULT_TYPE, VarTypes, Str, Array as _Array, CustomClass
from core.errors import MessageError, ParseError, CodegenError, CodegenWarning
from core.parsing import Builtins, Dunders
from core.operators import BUILTIN_UNARY_OP
from core.mangling import mangle_args, mangle_types, mangle_funcname, mangle_optional_args


class LLVMCodeGenerator(object):
    def __init__(self):
        """Initialize the code generator.
        This creates a new LLVM module into which code is generated. The
        generate_code() method can be called multiple times. It adds the code
        generated for this node into the module, and returns the IR value for
        the node.
        At any time, the current LLVM module being constructed can be obtained
        from the module attribute.
        """
        self.module = ir.Module()

        # Current IR builder.
        self.builder = None

        # Manages a symbol table while a function is being codegen'd.
        # Maps var names to ir.Value.
        self.func_symtab = {}

        # Decorator stack for whatever function is currently in context.
        self.func_decorators = []

        # Holds class definitions for codegen.
        self.class_symtab = {}

        # Holds a stack of loop exits.
        # Used to track where to break out of a loop.
        self.loop_exit = []

        # Holds functions that have optional arguments.
        # This allows them to be looked up efficiently.

        self.opt_args_funcs = {}

        # Set up pointer size and ptr_size vartype for current hardware.
        self.pointer_size = (ir.PointerType(VarTypes.u8).get_abi_size(
            llvm.create_target_data(self.module.data_layout)))

        self.pointer_bitwidth = self.pointer_size * 8

        from core.vartypes import UnsignedInt
        VarTypes['ptr_size'] = UnsignedInt(self.pointer_bitwidth)
        # XXX: this causes ALL instances of .ptr_size
        # in the environment instance
        # to be set to the platform width!
        # we may not want to have this behavior

    def _int(self, pyval):
        '''
        Returns a constant for Python int value.
        Used for gep, so it returns a value that is the bitwidth
        of the pointer size for the needed architecture.
        '''
        return ir.Constant(VarTypes.ptr_size, int(pyval))

    def _i32(self, pyval):
        '''
        Returns a constant for Python int value.
        Used for gep, so it returns a value that is the bitwidth
        of the pointer size for the needed architecture.
        '''
        return ir.Constant(VarTypes.u32, int(pyval))

    def generate_code(self, node):
        assert isinstance(node, (Prototype, Function, Uni, Class))
        return self._codegen(node, False)

    def _isize(self):
        '''
        Returns a constant of the pointer size for the currently configured architecture.
        The size is obtained from the LLVMCodeGenerator object, and is set when
        that object is instantiated. By default it's the pointer size for the current
        hardware, but you will be able to override it later.
        '''
        return ir.Constant(VarTypes.ptr_size, self.pointer_size)

    def _obj_size_type(self, obj=None):
        return obj.get_abi_size(
            llvm.create_target_data(self.module.data_layout))

    def _obj_size(self, obj):
        return self._obj_size_type(obj.type)

    def _alloca(self, name, type=None, size=None):
        """Create an alloca in the entry BB of the current function."""
        assert type is not None
        with self.builder.goto_entry_block():
            alloca = self.builder.alloca(type, size=size, name=name)
        return alloca

    def _codegen(self, node, check_for_type=True):
        """Node visitor. Dispatches upon node type.
        For AST node of class Foo, calls self._codegen_Foo. Each visitor is
        expected to return a llvmlite.ir.Value.
        """
        method = '_codegen_' + node.__class__.__name__
        result = getattr(self, method)(node)

        if check_for_type and not hasattr(result, 'type'):
            raise CodegenError(
                f'Expression does not return a value along all code paths, or expression returns an untyped value',
                node.position)

        return result

    def _codegen_NoneType(self, node):
        pass

    def _codegen_Number(self, node):
        num = ir.Constant(node.vartype, node.val)
        return num

    def _codegen_VariableType(self, node):
        return node.vartype

    def _varaddr(self, node, report=True):
        if report:
            name = node.name
        else:
            name = node
        v = self.func_symtab.get(name)
        if v is None:
            v = self.module.globals.get(name)
        if v is None:
            if not report:
                return None
            raise CodegenError(f"Undefined variable: {node.name}",
                               node.position)
        return v

    def _codegen_Return(self, node):
        '''
        Generates a return from within a function, and 
        sets the `self.func_returncalled` flag
        to notify that a return has been triggered.
        '''

        retval = self._codegen(node.val)
        if self.func_returntype is None:
            raise CodegenError(f'Unknown return declaration error',
                               node.position)

        if retval.type != self.func_returntype:
            raise CodegenError(
                f'Expected return type "{self.func_returntype.descr()}" but got "{retval.type.descr()}" instead',
                node.val.position)

        self.builder.store(retval, self.func_returnarg)
        self.builder.branch(self.func_returnblock)
        self.func_returncalled = True

    def _codegen_ArrayElement(self, node, array):
        '''
        Returns a pointer to the requested element of an array.
        '''

        accessor = [
            self._int(0),
        ] + [self._codegen(n) for n in node.elements]

        # FIXME: This is intended to trap wrongly sized array accessors
        # but we should find a more elegant way to do it in the parsing
        # phase if possible

        try:
            ptr = self.builder.gep(array, accessor, True, f'{array.name}')
        except (AttributeError, IndexError):
            raise CodegenError(
                f'Invalid array accessor for "{array.name}" (maybe wrong number of dimensions?)',
                node.position)

        return ptr

    def _codegen_Variable(self, node, noload=False):

        current_node = node
        previous_node = None

        # At the bottom of each iteration of the loop,
        # we should return a DIRECT pointer to an object

        while True:

            if previous_node is None and isinstance(current_node, Variable):
                if isinstance(getattr(current_node, 'child', None), (Call,)):
                    previous_node = current_node
                    current_node = current_node.child
                    continue

                latest = self._varaddr(current_node)
                current_load = not latest.type.is_obj_ptr()

            elif isinstance(current_node, ArrayAccessor):
                # eventually this will be coded as a call
                # to __index__ method of the element in question
                #
                if not isinstance(latest, ir.instructions.LoadInstr):
                    array_element = self.builder.alloca(latest.type)
                    self.builder.store(latest, array_element)
                else:
                    if latest.type.is_obj_ptr():
                        array_element = latest
                    else:
                        array_element = self._varaddr(previous_node)

                latest = self._codegen_ArrayElement(
                    current_node, array_element)
                current_load = not latest.type.is_obj_ptr()

            elif isinstance(current_node, Call):
                # eventually, when we have function pointers,
                # we'll need to have a pattern here similar to how
                # we handle ArrayAccessors above

                latest = self._codegen_Call(current_node)
                current_load = False
                # TODO: why is a call the exception?

            elif isinstance(current_node, Variable):
                try:
                    oo = latest.type.pointee
                except AttributeError:
                    raise CodegenError(f'Not a pointer or object',
                                       current_node.position)

                _latest_vid = oo.v_id
                _cls = self.class_symtab[_latest_vid]
                _pos = _cls.v_types[current_node.name]['pos']

                # for some reason we can't use i64 gep here

                index = [
                    self._i32(0),
                    self._i32(_pos)
                ]                

                latest = self.builder.gep(
                    latest, index, True,
                    previous_node.name + '.' + current_node.name)

                current_load = not latest.type.is_obj_ptr()

            # pathological case
            else:
                raise CodegenError(
                    f'Unknown variable instance', current_node.position
                )

            child = getattr(current_node, 'child', None)
            if child is None:
                break

            if current_load:
                latest = self.builder.load(latest, node.name+'.accessor')

            previous_node = current_node
            current_node = child

        if noload is True:
            return latest

        if current_load:
            return self.builder.load(latest, node.name)
        else:
            return latest

    def _codegen_Class(self, node):
        self.class_symtab[node.name] = node.vartype

    def _codegen_Assignment(self, lhs, rhs):
        if not isinstance(lhs, Variable):
            raise CodegenError(f'Left-hand side of expression is not a variable and cannot be assigned a value at runtime',
                               lhs.position)

        ptr = self._codegen_Variable(lhs, noload=True)
        if getattr(ptr, 'global_constant', None):
            raise CodegenError(
                f'Universal constant "{lhs.name}" cannot be reassigned',
                lhs.position)

        value = self._codegen(rhs)

        if ptr.type.pointee != value.type:
            if getattr(lhs, 'accessor', None):
                raise CodegenError(
                    f'Cannot assign value of type "{value.type.descr()}" to element of array "{ptr.pointer.name}" of type "{ptr.type.pointee.descr()}"',
                    rhs.position)
            else:
                raise CodegenError(
                    f'Cannot assign value of type "{value.type.descr()}" to variable "{ptr.name}" of type "{ptr.type.pointee.descr()}"',
                    rhs.position)

        self.builder.store(value, ptr)
        return value

    def _codegen_String(self, node):
        return self._string_base(node.val)

    def _string_base(self, string, global_constant=True):
        '''
        Core function for code generation for strings.
        This will also be called when we create strings dynamically
        in the course of a function, or statically during compilation.
        '''
        # only strings codegenned from source should be stored as LLVM globals
        module = self.module
        string_length = len(string.encode('utf8')) + 1
        type = ir.ArrayType(ir.IntType(8), string_length)

        str_name = f'.str.{len(module.globals)}'

        # Create the LLVM constant value for the underlying string data.

        str_const = ir.GlobalVariable(module, type, str_name + '.dat')
        str_const.storage_class = 'private'
        str_const.unnamed_addr = True
        str_const.global_constant = True

        str_const.initializer = ir.Constant(
            type,
            bytearray(string, 'utf8') + b'\x00')

        # Get pointer to first element in string's byte array
        # and bitcast it to a ptr i8.

        spt = str_const.gep([self._int(0)]).bitcast(VarTypes.u8.as_pointer())

        # Create the string object that points to the constant.

        str_val = ir.GlobalVariable(module, VarTypes.str, str_name)
        str_val.storage_class = 'private'
        str_val.unnamed_addr = True
        str_val.global_constant = True

        str_val.initializer = VarTypes.str(
            [spt, ir.Constant(VarTypes.u32, string_length)])

        return str_val

    def _codegen_methodcall(self, node, lhs, rhs):
        func = self.module.globals.get(
            f'binary.{node.op}{mangle_args((lhs.type,rhs.type))}')
        if func is None:
            raise NotImplementedError
        return self.builder.call(func, [lhs, rhs], 'userbinop')

    def _codegen_Binary(self, node):
        # Assignment is handled specially because it doesn't follow the general
        # recipe of binary ops.

        if node.op == '=':
            return self._codegen_Assignment(node.lhs, node.rhs)

        lhs = self._codegen(node.lhs)
        rhs = self._codegen(node.rhs)

        if lhs.type != rhs.type:
            raise CodegenError(
                f'"{lhs.type.descr()}" ({node.lhs.name}) and "{rhs.type.descr()}" ({node.rhs.name}) are incompatible types for operation',
                node.position)
        else:
            vartype = lhs.type
            v_type = getattr(lhs.type, 'v_type', None)

        try:
            # For non-primitive types we need to look up the property

            if v_type is not None:
                if v_type == Str:
                    raise NotImplementedError

            # TODO: no overflow checking!
            # we have to add that when we have exceptions, etc.
            # with fcmp_ordered this is assuming we are strictly comparing
            # float to float in all cases.

            # Integer operations

            if isinstance(vartype, ir.IntType):

                if lhs.type.signed:
                    signed_op = self.builder.icmp_signed
                else:
                    signed_op = self.builder.icmp_unsigned

                if node.op == '+':
                    return self.builder.add(lhs, rhs, 'addop')
                elif node.op == '-':
                    return self.builder.sub(lhs, rhs, 'subop')
                elif node.op == '*':
                    return self.builder.mul(lhs, rhs, 'multop')
                elif node.op == '<':
                    x = signed_op('<', lhs, rhs, 'ltop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '>':
                    x = signed_op('>', lhs, rhs, 'gtop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '>=':
                    x = signed_op('>=', lhs, rhs, 'gteqop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '<=':
                    x = signed_op('<=', lhs, rhs, 'lteqop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '==':
                    x = signed_op('==', lhs, rhs, 'eqop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '!=':
                    x = signed_op('!=', lhs, rhs, 'neqop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '/':
                    if int(getattr(rhs, 'constant', 1)) == 0:
                        raise CodegenError('Integer division by zero',
                                           node.rhs.position)
                    return self.builder.sdiv(lhs, rhs, 'divop')
                elif node.op == 'and':
                    x = self.builder.and_(lhs, rhs, 'andop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == 'or':
                    x = self.builder.or_(lhs, rhs, 'orop')
                    x.type = VarTypes.bool
                    return x
                else:
                    return self._codegen_methodcall(node, lhs, rhs)

            # floating-point operations

            elif isinstance(vartype, (ir.DoubleType, ir.FloatType)):

                if node.op == '+':
                    return self.builder.fadd(lhs, rhs, 'faddop')
                elif node.op == '-':
                    return self.builder.fsub(lhs, rhs, 'fsubop')
                elif node.op == '*':
                    return self.builder.fmul(lhs, rhs, 'fmultop')
                elif node.op == '/':
                    return self.builder.fdiv(lhs, rhs, 'fdivop')
                elif node.op == '<':
                    cmp = self.builder.fcmp_ordered('<', lhs, rhs, 'fltop')
                    return self.builder.uitofp(cmp, vartype, 'fltoptodouble')
                elif node.op == '>':
                    cmp = self.builder.fcmp_ordered('>', lhs, rhs, 'fgtop')
                    return self.builder.uitofp(cmp, vartype, 'flgoptodouble')
                elif node.op == '>=':
                    cmp = self.builder.fcmp_ordered('>=', lhs, rhs, 'fgeqop')
                    return self.builder.uitofp(cmp, vartype, 'fgeqopdouble')
                elif node.op == '<=':
                    cmp = self.builder.fcmp_ordered('<=', lhs, rhs, 'fleqop')
                    return self.builder.uitofp(cmp, vartype, 'fleqopdouble')
                elif node.op == '==':
                    x = self.builder.fcmp_ordered('==', lhs, rhs, 'feqop')
                    x.type = VarTypes.bool
                    return x
                elif node.op == '!=':
                    x = self.builder.fcmp_ordered('!=', lhs, rhs, 'fneqop')
                    x.type = VarTypes.bool
                    return x
                elif node.op in ('and', 'or'):
                    raise CodegenError(
                        'Operator not supported for "float" or "double" types',
                        node.lhs.position)
                else:
                    return self._codegen_methodcall(node, lhs, rhs)

            # Pointer equality

            elif isinstance(vartype, ir.PointerType):
                # LATER: use vartype.is_obj_ptr() to determine if this is a complex object that needs to invoke its __eq__ method, but this is fine for now
                signed_op = self.builder.icmp_unsigned
                if isinstance(rhs.type, ir.PointerType):
                    if node.op == '==':
                        x = signed_op('==', lhs, rhs, 'eqptrop')
                        x.type = VarTypes.bool
                        return x

            else:
                return self._codegen_methodcall(node, lhs, rhs)

        except NotImplementedError:
            raise CodegenError(
                f'Unknown binary operator {node.op} for {vartype}',
                node.position)

    def _codegen_Match(self, node):
        cond_item = self._codegen(node.cond_item)
        default = ir.Block(self.builder.function, 'defaultmatch')
        exit = ir.Block(self.builder.function, 'endmatch')
        switch_instr = self.builder.switch(cond_item, default)
        cases = []
        exprs = {}
        for value, expr, in node.match_list:
            val_codegen = self._codegen(value)
            if not isinstance(val_codegen, ir.values.Constant):
                raise CodegenError(
                    f'Match parameter must be a constant, not an expression',
                    value.position)
            if val_codegen.type != cond_item.type:
                raise CodegenError(
                    f'Type of match object ("{cond_item.type.descr()}") and match parameter ("{val_codegen.type.descr()}") must be consistent)',
                    value.position)
            if expr in exprs:
                switch_instr.add_case(val_codegen, exprs[expr])
            else:
                n = ir.Block(self.builder.function, 'match')
                switch_instr.add_case(val_codegen, n)
                exprs[expr] = n
                cases.append([n, expr])
        for block, expr in cases:
            self.builder.function.basic_blocks.append(block)
            self.builder.position_at_start(block)
            result = self._codegen(expr, False)
            if result:
                self.builder.branch(exit)
        self.builder.function.basic_blocks.append(default)
        self.builder.position_at_start(default)
        if node.default:
            self._codegen(node.default, False)
        self.builder.branch(exit)
        self.builder.function.basic_blocks.append(exit)
        self.builder.position_at_start(exit)
        return cond_item

    def _codegen_When(self, node):
        return self._codegen_If(node, True)
        # we're going to modify If to support both If and When

    def _codegen_If(self, node, codegen_when=False):
        # Emit comparison value

        cond_val = self._codegen(node.cond_expr)

        type = cond_val.type

        cond = ('!=', cond_val, ir.Constant(type, 0), 'notnull')

        if isinstance(type, (ir.FloatType, ir.DoubleType)):
            cmp = self.builder.fcmp_unordered(*cond)
        elif isinstance(type, SignedInt):
            cmp = self.builder.icmp_signed(*cond)
        else:
            cmp = self.builder.icmp_unsigned(*cond)

        # Create basic blocks to express the control flow
        then_bb = ir.Block(self.builder.function, 'then')
        else_bb = ir.Block(self.builder.function, 'else')
        merge_bb = ir.Block(self.builder.function, 'endif')

        # branch to either then_bb or else_bb depending on cmp
        # if no else, then go straight to merge
        if node.else_expr is None:
            self.builder.cbranch(cmp, then_bb, merge_bb)
        else:
            self.builder.cbranch(cmp, then_bb, else_bb)

        # Emit the 'then' part
        self.builder.function.basic_blocks.append(then_bb)
        self.builder.position_at_start(then_bb)

        self.breaks = False

        then_val = self._codegen(node.then_expr, False)
        if then_val:
            self.builder.branch(merge_bb)

        # Emission of then_val could have generated a new basic block
        # (and thus modified the current basic block).
        # To properly set up the PHI, remember which block the 'then' part ends in.
        then_bb = self.builder.block

        # Emit the 'else' part, if needed

        if node.else_expr is None:
            else_val = None
        else:
            self.builder.function.basic_blocks.append(else_bb)
            self.builder.position_at_start(else_bb)
            else_val = self._codegen(node.else_expr)
            if else_val:
                self.builder.branch(merge_bb)
            else_bb = self.builder.block

        # check for an early return,
        # prune unneeded phi operations

        self.builder.function.basic_blocks.append(merge_bb)
        self.builder.position_at_start(merge_bb)

        if then_val is None and else_val is None:
            # returns are present in each branch
            return
        elif not else_val:
            # return present in 1st branch only
            return then_val.type
        elif not then_val:
            # return present in 2nd branch only
            return else_val.type
        # otherwise no returns in any branch

        if codegen_when:
            return cond_val

        # make sure then/else are in agreement
        # so we're returning consistent types

        if then_val.type != else_val.type:
            raise CodegenError(
                f'"then/else" expression return types must be the same ("{then_val.type.descr()}" does not match "{else_val.type.descr()}"',
                node.position)

        phi = self.builder.phi(then_val.type, 'ifval')
        phi.add_incoming(then_val, then_bb)
        phi.add_incoming(else_val, else_bb)
        return phi

    def _codegen_Break(self, node):
        exit = self.loop_exit.pop()
        self.breaks = True
        self.builder.branch(exit)

    def _codegen_Loop(self, node):
        # Output this as:
        #   ...
        #   start = startexpr
        #   goto loopcond
        # loopcond:
        #   variable = phi [start, loopheader], [nextvariable, loopbody]
        #   step = stepexpr (or variable + 1)
        #   nextvariable = step
        #   endcond = endexpr
        #   br endcond, loopbody, loopafter
        # loopbody:
        #   bodyexpr
        #   jmp loopcond
        # loopafter:
        #   return variable

        # Define blocks
        loopcond_bb = ir.Block(self.builder.function, 'loopcond')
        loopbody_bb = ir.Block(self.builder.function, 'loopbody')
        loopafter_bb = ir.Block(self.builder.function, 'loopafter')

        # If this loop has no conditions, codegen it with a manual exit

        if node.start_expr is None:
            self.builder.branch(loopbody_bb)
            self.builder.function.basic_blocks.append(loopbody_bb)
            self.builder.position_at_start(loopbody_bb)
            self.loop_exit.append(loopafter_bb)
            self._codegen(node.body, False)
            self.builder.branch(loopbody_bb)
            self.builder.function.basic_blocks.append(loopafter_bb)
            self.builder.position_at_start(loopafter_bb)
            return

        # ###########
        # loop header
        #############

        var_addr = self._varaddr(node.start_expr.name, False)
        if var_addr is None:
            self._codegen_Var(Var(node.start_expr.position, [node.start_expr]))
            var_addr = self._varaddr(node.start_expr.name, False)
        else:
            self._codegen_Assignment(node.start_expr,
                                     node.start_expr.initializer)

        loop_ctr_type = var_addr.type.pointee

        # Jump to loop cond
        self.builder.branch(loopcond_bb)

        ###########
        # loop cond
        ###########

        self.builder.function.basic_blocks.append(loopcond_bb)
        self.builder.position_at_start(loopcond_bb)

        # Set the symbol table to to reach de local counting variable.
        # If it shadows an existing variable, save it before and restore it later.
        oldval = self.func_symtab.get(node.start_expr.name)
        self.func_symtab[node.start_expr.name] = var_addr

        # Compute the end condition
        endcond = self._codegen(node.end_expr)

        # TODO: this requires different comparison operators
        # based on the type of the loop object - int vs. float, chiefly
        # this is a pattern we may repeat too often

        cond = ('!=', endcond, ir.Constant(loop_ctr_type, 0), 'loopifcond')

        if isinstance(loop_ctr_type, (ir.FloatType, ir.DoubleType)):
            cmp = self.builder.fcmp_unordered(*cond)
        elif isinstance(loop_ctr_type, ir.IntType):
            if getattr(loop_ctr_type, 'v_signed', None):
                cmp = self.builder.icmp_signed(*cond)
            else:
                cmp = self.builder.icmp_unsigned(*cond)

        # Goto loop body if condition satisfied, otherwise, exit.
        self.builder.cbranch(cmp, loopbody_bb, loopafter_bb)

        ############
        # loop body
        ############

        self.builder.function.basic_blocks.append(loopbody_bb)
        self.builder.position_at_start(loopbody_bb)

        # Emit the body of the loop.
        # Note that we ignore the value computed by the body.
        self._codegen(node.body, False)

        # If the step is unknown, make it increment by 1
        if node.step_expr is None:
            node.step_expr = Binary(node.position, "+",
                                    Variable(node.position,
                                             node.start_expr.name),
                                    Number(None, 1, loop_ctr_type))

        # Evaluate the step and update the counter
        nextval = self._codegen(node.step_expr)
        self.builder.store(nextval, var_addr)

        # Goto loop cond
        self.builder.branch(loopcond_bb)

        #############
        # loop after
        #############

        # New code will be inserted into a new block
        self.builder.function.basic_blocks.append(loopafter_bb)
        self.builder.position_at_start(loopafter_bb)

        # Remove the loop variable from the symbol table;
        # if it shadowed an existing variable, restore that.
        if oldval is None:
            del self.func_symtab[node.start_expr.name]
        else:
            self.func_symtab[node.start_expr.name] = oldval

        # The 'loop' expression returns the last value of the counter
        return self.builder.load(var_addr)

    def _codegen_While(self, node):
        # This is a modified version of a For.

        # Define blocks
        loopcond_bb = ir.Block(self.builder.function, 'loopcond')
        loopbody_bb = ir.Block(self.builder.function, 'loopbody')
        loopafter_bb = ir.Block(self.builder.function, 'loopafter')

        # ###########
        # loop header
        #############

        # Save the current block to tell the loop cond where we are coming from
        # no longer needed, I think
        #loopheader_bb = self.builder.block

        # Jump to loop cond
        self.builder.branch(loopcond_bb)

        ###########
        # loop cond
        ###########

        self.builder.function.basic_blocks.append(loopcond_bb)
        self.builder.position_at_start(loopcond_bb)

        # Compute the end condition
        endcond = self._codegen(node.cond_expr)

        type = endcond.type

        # TODO: this requires different comparison operators
        # based on the type of the loop object - int vs. float, chiefly
        # this is a pattern we may repeat too often

        cond = ('!=', endcond, ir.Constant(type, 0), 'loopcond')

        if isinstance(type, (ir.FloatType, ir.DoubleType)):
            cmp = self.builder.fcmp_unordered(*cond)
        elif isinstance(type, ir.IntType):
            if getattr(type, 'v_signed', None):
                cmp = self.builder.icmp_signed(*cond)
            else:
                cmp = self.builder.icmp_unsigned(*cond)

        # Goto loop body if condition satisfied, otherwise, exit.
        self.builder.cbranch(cmp, loopbody_bb, loopafter_bb)

        ############
        # loop body
        ############

        self.builder.function.basic_blocks.append(loopbody_bb)
        self.builder.position_at_start(loopbody_bb)

        # Emit the body of the loop.
        body_val = self._codegen(node.body, False)

        # The value of the body has to be placed into a special
        # return variable so it's valid across all code paths
        self.builder.position_at_start(loopcond_bb)
        return_var = self.builder.alloca(
            body_val.type, size=None, name='%_while_loop_return')

        # Goto loop cond
        self.builder.position_at_end(loopbody_bb)
        self.builder.store(body_val, return_var)
        self.builder.branch(loopcond_bb)

        #############
        # loop after
        #############

        # New code will be inserted into a new block
        self.builder.function.basic_blocks.append(loopafter_bb)
        self.builder.position_at_start(loopafter_bb)

        # The 'while' expression returns the value of the body
        return self.builder.load(return_var)

    def _codegen_Call(self, node, obj_method=False):
        if not obj_method:
            if node.name in Builtins:
                return getattr(self, '_codegen_Builtins_' + node.name)(node)
            if node.name in Dunders:
                return self._codegen_dunder_methods(node)

        call_args = []
        possible_opt_args_funcs = []

        for arg in node.args:
            call_args.append(self._codegen(arg))
            _ = mangle_types(node.name, call_args)
            if _ in self.opt_args_funcs:
                possible_opt_args_funcs.append(self.opt_args_funcs[_])

        # XXX: we might need to move this BEFORE vararg checking

        if obj_method:
            node.name = f'{call_args[0].type.pointee.name}.__{node.name}__'

        if not possible_opt_args_funcs:
            mangled_name = mangle_types(node.name, call_args)
            callee_func = self.module.globals.get(mangled_name, None)

        else:
            try:
                match = False
                for f1 in possible_opt_args_funcs:
                    if len(call_args) > len(f1.args):
                        continue
                    match = True
                    for function_arg, call_arg in zip(f1.args, call_args):
                        if function_arg.type != call_arg.type:
                            match = False
                            break
                if not match:
                    raise TypeError
            except TypeError:
                raise ParseError(
                    f'argument types do not match possible argument signature for optional-argument function "{f1.public_name}"',
                    node.position
                )
            else:
                callee_func = f1
                for n in range(len(call_args), len(f1.args)):
                    call_args.append(f1.args[n].default_value)

        if not callee_func:
            callee_func = self.module.globals.get(node.name, None)

        if callee_func is None or not isinstance(callee_func, ir.Function):
            raise CodegenError(
                f'Call to unknown function "{node.name}" with signature "{[n.type.descr() for n in call_args]}" (maybe this call signature is not implemented for this function?)',
                node.position)

        if len(callee_func.args) != len(call_args):
            raise CodegenError(
                f'Call argument length mismatch for "{callee_func.public_name}" (expected {len(callee_func.args)}, got {len(node.args)})',
                node.position)

        for x, n in enumerate(zip(call_args, callee_func.args)):
            if n[0].type != n[1].type:
                raise CodegenError(
                    f'Call argument type mismatch for "{callee_func.public_name}" (position {x}: expected {n[1].type.descr()}, got {n[0].type.descr()})',
                    node.args[x].position)

        return self.builder.call(callee_func, call_args, 'calltmp')

    def _codegen_Prototype(self, node):
        funcname = node.name

        # Create a function type

        vartypes = []
        vartypes_with_defaults = []

        append_to = vartypes

        for x in node.argnames:
            # TODO: move this to a _codegen_vartype operation
            arg_type = x.vartype
            if isinstance(arg_type, Array):
                s = arg_type.element_type
                for n in arg_type.elements_elements:
                    s = VarTypes.array(s, int(n.val))
            else:
                s = arg_type
            if x.initializer is not None:
                append_to = vartypes_with_defaults
            append_to.append(s)

        # TODO: it isn't yet possible to have an implicitly
        # typed function that just uses the return type of the body
        # we might be able to do this by way of a special call
        # to this function
        # note that Extern functions MUST be typed

        if node.vartype is None:
            node.vartype = DEFAULT_TYPE

        functype = ir.FunctionType(node.vartype,
                                   vartypes+vartypes_with_defaults)

        public_name = funcname

        opt_args = None

        linkage = None

        # TODO: identify anonymous functions with a property
        # not by way of their nomenclature

        if node.extern is False and not funcname.startswith(
                '_ANONYMOUS.') and funcname != 'main':
            linkage = 'private'
            if len(vartypes) > 0:
                funcname = public_name + mangle_args(vartypes)

            required_args = funcname

            if len(vartypes_with_defaults) > 0:
                opt_args = mangle_optional_args(vartypes_with_defaults)
                funcname += opt_args

        # If a function with this name already exists in the module...
        if funcname in self.module.globals:

            # We only allow the case in which a declaration exists and now the
            # function is defined (or redeclared) with the same number of args.
            # TODO: I think this rule should be dropped and ANY prior
            # function version should never be overridden
            func = existing_func = self.module.globals[funcname]

            if not isinstance(existing_func, ir.Function):
                raise CodegenError(f'Function/universal name collision {funcname}',
                                   node.position)
            if not existing_func.is_declaration:
                raise CodegenError(
                    f'Redefinition of {funcname}', node.position)
            if len(existing_func.function_type.args) != len(functype.args):
                raise CodegenError(
                    f'Redefinition of {funcname} with different number of arguments',
                    node.position)
        else:
            # Otherwise create a new function

            func = ir.Function(self.module, functype, funcname)

            # Name the arguments
            for i, arg in enumerate(func.args):
                arg.name = node.argnames[i].name

        if opt_args is not None:
            self.opt_args_funcs[required_args] = func

        # Set defaults (if any)

        for x, n in enumerate(node.argnames):
            if n.initializer is not None:
                func.args[x].default_value = self._codegen(
                    n.initializer, False)

        func.public_name = public_name

        # Set LLVM function attributes

        # Calling convention.
        # This is the default with no varargs

        func.calling_convention = 'fastcc'

        # Linkage.
        # Default is 'private' if it's not extern, an anonymous function, or main

        if linkage:
            func.linkage = linkage

        # Address is not relevant by default
        func.unnamed_addr = True

        # Inlining. Operator functions are inlined by default.
        if node.isoperator:
            func.attributes.add('alwaysinline')
        else:
            func.attributes.add('noinline')

        # Attributes.

        # External calls, by default, no recursion
        if node.extern:
            func.attributes.add('norecurse')

        # By default, no lazy binding
        func.attributes.add('nonlazybind')

        # By default, no stack unwinding
        func.attributes.add('nounwind')

        # Reset the decorator list now that we're done with it
        self.func_decorators = []

        return func

    def _codegen_Function(self, node):

        # Reset the symbol table. Prototype generation will pre-populate it with
        # function arguments.
        self.func_symtab = {}

        # Create the function skeleton from the prototype.
        func = self._codegen(node.proto, False)

        # Create the entry BB in the function and set a new builder to it.
        bb_entry = func.append_basic_block('entry')
        self.builder = ir.IRBuilder(bb_entry)

        self.func_returncalled = False
        self.func_returntype = func.return_value.type
        self.func_returnblock = func.append_basic_block('exit')
        self.func_returnarg = self._alloca('%_return', self.func_returntype)

        # Add all arguments to the symbol table and create their allocas
        for _, arg in enumerate(func.args):
            if arg.type.is_obj_ptr():  # is_obj(arg.type):
                alloca = arg
            else:
                alloca = self._alloca(arg.name, arg.type)
                self.builder.store(arg, alloca)

            # We don't shadow existing variables names, ever
            assert not self.func_symtab.get(
                arg.name) and "arg name redefined: " + arg.name
            self.func_symtab[arg.name] = alloca

        # Generate code for the body
        retval = self._codegen(node.body, False)

        if retval is None and self.func_returncalled is True:
            # we don't need to check for a final returned value,
            # because it's implied that there's an early return
            pass
        else:
            if not hasattr(retval, 'type'):
                raise CodegenError(
                    f'Function "{node.proto.name}" has a return value of type "{func.return_value.type.descr()}" but no concluding expression with an explicit return type was supplied',
                    node.position)

            if retval is None and func.return_value.type is not None:
                raise CodegenError(
                    f'Function "{node.proto.name}" has a return value of type "{func.return_value.type.descr()}" but no expression with an explicit return type was supplied',
                    node.position)

            if func.return_value.type != retval.type:
                if node.proto.name.startswith(_ANONYMOUS):
                    func.return_value.type = retval.type
                    self.func_returnarg = self._alloca('%_return', retval.type)
                else:
                    raise CodegenError(
                        f'Prototype for function "{node.proto.name}" has return type "{func.return_value.type.descr()}", but returns "{retval.type.descr()}" instead (maybe an implicit return?)',
                        node.proto.position)

            self.builder.store(retval, self.func_returnarg)
            self.builder.branch(self.func_returnblock)

        # for n in self.func_symtab.values():
        #     if n.heap_alloc:
        #         call obj destructor
        #         each obj in turn calls any destructors it might have

        self.builder = ir.IRBuilder(self.func_returnblock)

        self.builder.ret(self.builder.load(self.func_returnarg))

        self.func_returntype = None
        self.func_returnarg = None
        self.func_returnblock = None
        self.func_returncalled = None

    def _codegen_Unary(self, node):
        operand = self._codegen(node.rhs)
        # TODO: no overflow checking yet!
        if node.op in BUILTIN_UNARY_OP:
            if node.op == 'not':
                if isinstance(operand.type, (ir.IntType, ir.DoubleType)):
                    cond_expr = Binary(node.position, '==', node.rhs,
                                       Number(node.position, 0, operand.type))
                    return self._codegen_If(
                        If(
                            node.position,
                            cond_expr,
                            Number(node.position, 1, operand.type),
                            Number(node.position, 0, operand.type), ))
            elif node.op == '-':
                lhs = ir.Constant(operand.type, 0)
                if isinstance(operand.type, ir.IntType):
                    return self.builder.sub(lhs, operand, 'negop')
                elif isinstance(operand.type, ir.DoubleType):
                    return self.builder.fsub(lhs, operand, 'fnegop')
        else:
            func = self.module.globals.get(
                f'unary.{node.op}{mangle_args((operand.type,))}')
            if not func:
                raise CodegenError(
                    f'Undefined unary operator "{node.op}" for "{operand.type.descr()}"',
                    node.position)
            return self.builder.call(func, [operand], 'unop')

    def _codegen_Var(self, node):
        for v in node.vars:

            name = v.name
            type = v.vartype
            expr = v.initializer
            position = v.position

            val, type = self._codegen_VarDef(expr, type)

            var_ref = self.func_symtab.get(name)
            if var_ref is not None:
                raise CodegenError(f'"{name}" already defined in local scope',
                                   position)

            var_ref = self.module.globals.get(name, None)
            if var_ref is not None:
                raise CodegenError(
                    f'"{name}" already defined in universal scope', position)

            var_ref = self._alloca(name, type)
            self.func_symtab[name] = var_ref

            if expr:

                # if _no_alloca is set, we've already preallocated space
                # for the object, so all we have to do is set the name
                # to its existing pointer

                if val.no_alloca:
                    self.func_symtab[name] = val
                else:
                    self.builder.store(val, var_ref)
            else:
                if type.is_obj_ptr():
                    # allocate the actual object, not just a pointer to it
                    # beacuse it doesn't actually exist yet!
                    obj = self._alloca('obj', type.pointee)
                    self.builder.store(obj, var_ref)

    def _codegen_VarDef(self, expr, vartype):
        if expr is None:
            val = None

            if isinstance(vartype, Class):
                # XXX: using .v_id may not be the smart way
                # to do this - we need to figure out exactly
                # which name to use, but for now it seems to work
                final_type = self.class_symtab[vartype.v_id]

            elif isinstance(vartype, Array):
                t = vartype.element_type

                dims = []
                for n in (vartype.elements.elements):
                    if isinstance(n, Variable):
                        v = self.module.globals.get(n.name, None)
                        if not v:
                            raise CodegenError(
                                f'"{n.name}" could not be found in the universal scope to be used as an array size definition (is it defined afterwards?)',
                                n.position)
                        i = getattr(v, 'initializer', None)
                        if not i:
                            raise CodegenError(
                                f'Array sizes cannot be described by an uninitialized variable in the universal scope',
                                vartype.position)
                        if not isinstance(i.type, ir.IntType):
                            raise CodegenError(
                                f'Array sizes can only be set as integer types',
                                vartype.position)
                        c = int(getattr(i, 'constant', None))
                        dim = c
                    else:
                        try:
                            dim = int(n.val)
                        except ValueError:
                            raise CodegenError(
                                f'Array sizes must be integer constants',
                                vartype.position)
                    dims.append(dim)
                    t = VarTypes.array(t, dim)

                final_type = t

            else:
                if vartype is None:
                    vartype = DEFAULT_TYPE
                final_type = vartype
        else:
            val = self._codegen(expr)

            if vartype is None:
                vartype = val.type

            if vartype == ir.types.FunctionType:
                pass
                # instead of conventional codegen, we generate the fp here

            if val.type != vartype:
                raise CodegenError(
                    f'Type declaration and variable assignment type do not match (expected "{vartype.descr()}", got "{val.type.descr()}"',
                    expr.position)
            if val.type.signed != vartype.signed:
                raise CodegenError(
                    f'Type declaration and variable assignment type have signed/unsigned mismatch (expected "{vartype.descr()}", got "{val.type.descr()}")',
                    expr.position)

            final_type = val.type

        return val, final_type

    def _codegen_Uni(self, node, const=False):
        for name, vartype, expr, position in node.vars:
            var_ref = self.module.globals.get(name, None)

            if var_ref is not None:
                raise CodegenError(
                    f'Duplicate found in universal symbol table: "{name}"',
                    position)

            if const and expr is None:
                raise CodegenError(
                    f'Constants must have an assignment: "{name}"', position)

            val, final_type = self._codegen_VarDef(expr, vartype)

            if final_type is None:
                final_type = DEFAULT_TYPE

            str1 = ir.GlobalVariable(self.module, final_type, name)

            if const:
                str1.global_constant = True
            if val is None:
                str1.initializer = ir.Constant(final_type, None)
            else:
                str1.initializer = val

    def _codegen_Const(self, node):
        return self._codegen_Uni(node, True)

    def _codegen_Do(self, node):
        for n in node.expr_list:
            try:
                t = self._codegen(n, False)
            except CodegenError as e:
                raise e
        return t

    def _codegen_VarIn(self, node):
        old_bindings = []

        for v in node.vars.vars:

            name = v.name
            type = v.vartype
            init = v.initializer
            position = v.position

            # Emit the initializer before adding the variable to scope. This
            # prevents the initializer from referencing the variable itself.

            if self._varaddr(name, False) is not None:
                raise CodegenError(
                    f'Variable shadowing is not permitted; "{name}" is used in other scopes',
                    position)

            val, final_type = self._codegen_VarDef(init, type)

            var_addr = self._alloca(name, final_type)

            if val is not None:
                self.builder.store(val, var_addr)

            # Put var in symbol table; remember old bindings if any.
            old_bindings.append(self.func_symtab.get(name))
            self.func_symtab[name] = var_addr

        # Now all the vars are in scope. Codegen the body.
        body_val = self._codegen(node.body)

        # Restore the old bindings.
        for i, v in enumerate(node.vars.vars):
            name = v.name
            if old_bindings[i] is not None:
                self.func_symtab[name] = old_bindings[i]
            else:
                del self.func_symtab[name]

        return body_val

    def _codegen_dunder_methods(self, node):
        call = self._codegen_Call(
            Call(node.position, node.name,
                 node.args,
                 ),
            obj_method=True
        )
        return call

###########
# Builtins
###########

    def _check_pointer(self, obj, node):
        if not isinstance(obj.type, ir.PointerType):
            raise CodegenError('Parameter must be a pointer or object',
                               node.args[0].position)

    def _get_obj_noload(self, node, ptr_check=True):
        '''
        Returns a pointer to a codegenned object
        without a `load` instruction.
        '''
        arg = node.args[0]
        if isinstance(arg, Variable):
            codegen = self._codegen_Variable(arg, noload=True)
        else:
            codegen = self._codegen(arg)
        if ptr_check:
            self._check_pointer(codegen, node)
        return codegen

    def _codegen_Builtins_c_obj_alloc(self, node):
        '''
        Allocates bytes for an object of the type submitted.
        Eventually we will be able to submit a type directly.
        For now, use a throwaway closure
        E.g., for an i32[8]:
        var x=c_obj_alloc({with var z:i32[8] z})
        '''

        expr = self._codegen(node.args[0])
        sizeof = self._obj_size(expr)

        call = self._codegen_Call(
            Call(node.position, 'c_alloc',
                 [Number(node.position, sizeof, VarTypes.ptr_size)]))

        bc = self.builder.bitcast(call, expr.type.as_pointer())

        bc.no_alloca = True
        bc.heap_alloc = True

        return bc

    def _codegen_Builtins_c_obj_free(self, node):
        '''
        Deallocates memory for an object created with c_obj_alloc.
        '''
        expr = self._get_obj_noload(node)
        addr = self.builder.ptrtoint(expr, VarTypes.ptr_size)

        call = self._codegen_Call(
            Call(node.position, 'c_free',
                 [Number(node.position, addr.get_reference(), VarTypes.ptr_size)]))

        return call

    def _codegen_Builtins_c_obj_ref(self, node):
        '''
        Returns a typed pointer to the object.
        '''
        expr = self._get_obj_noload(node)
        s1 = self._alloca('obj_ref', expr.type)
        self.builder.store(expr, s1)
        return s1

    def _codegen_Builtins_c_size(self, node):
        '''
        Returns the size of the object's desciptor in bytes.
        For a string, this is NOT the size of the
        underlying string, but the size of the structure
        that describes a string.
        '''
        expr = self._codegen(node.args[0])

        if expr.type.is_obj_ptr():
            s1 = expr.type.pointee
        else:
            s1 = expr.type

        s2 = self._obj_size_type(s1)

        return ir.Constant(VarTypes.ptr_size, s2)

    def _codegen_Builtins_c_array_ptr(self, node):
        '''
        Returns a raw u8 pointer to the start of an array or structure.
        '''
        convert_from = self._get_obj_noload(node)
        gep = self.builder.gep(convert_from, [self._int(0)])
        bc = self.builder.bitcast(gep, VarTypes.u8.as_pointer())
        return bc

    def _codegen_Builtins_c_addr(self, node):
        '''
        Returns an unsigned value that is the address of the object in memory.
        '''
        address_of = self._get_obj_noload(node)
        return self.builder.ptrtoint(address_of, VarTypes.ptr_size)

        # perhaps we should also have a way to cast
        # c_addr as a pointer to a specific type (the reverse of this)

    def _codegen_Builtins_c_deref(self, node):
        '''
        Dereferences a pointer to a primitive, like an int.
        '''

        ptr = self._get_obj_noload(node)
        ptr2 = self.builder.load(ptr)

        if hasattr(ptr2.type, 'pointee'):
            ptr2 = self.builder.load(ptr2)

        if hasattr(ptr2.type, 'pointee'):
            raise CodegenError(
                f'"{node.args[0].name}" is not a reference to a scalar (use c_obj_deref for references to objects instead of scalars)',
                node.args[0].position)

        return ptr2

    def _codegen_Builtins_c_ref(self, node):
        '''
        Returns a typed pointer to a primitive, like an int.
        '''

        expr = self._get_obj_noload(node)

        if expr.type.is_obj_ptr():
            raise CodegenError(
                f'"{node.args[0].name}" is not a scalar (use c_obj_ref for references to objects instead of scalars)',
                node.args[0].position)

        return expr

    def _codegen_Builtins_c_obj_deref(self, node):
        '''
        Dereferences a pointer (itself passed as a pointer)
        and returns the object at the memory location.
        '''

        ptr = self._codegen(node.args[0])
        ptr2 = self.builder.load(ptr)
        self._check_pointer(ptr2, node)
        ptr3 = self.builder.load(ptr2)
        return ptr3

    def _codegen_Builtins_cast(self, node):
        '''
        Cast one data type as another, such as a pointer to a u64,
        or an i8 to a u32.
        Ignores signing.
        Does not truncate bitwidths, however.
        '''

        cast_from = self._codegen(node.args[0])
        cast_to = self._codegen(node.args[1], False)

        cast_exception = CodegenError(
            f'Casting from type "{cast_from.type.descr()}" to type "{cast_to.descr()}" is not supported',
            node.args[0].position)

        while True:

            # If we're casting FROM a pointer...

            if isinstance(cast_from.type, ir.PointerType):

                # it can't be an object pointer (for now)
                if cast_from.type.is_obj_ptr():
                    raise cast_exception

                # and it can't be anything other than an int
                if not isinstance(cast_to, ir.IntType):
                    raise cast_exception

                # and it has to be the same bitwidth
                if self.pointer_bitwidth != cast_to.width:
                    raise cast_exception

                op = self.builder.ptrtoint
                break

            # If we're casting TO a pointer ...

            if isinstance(cast_to, ir.PointerType):

                # it can't be from anything other than an int
                if not isinstance(cast_from.type, ir.IntType):
                    raise cast_exception

                # and it has to be the same bitwidth
                if cast_from.type.width != self.pointer_bitwidth:
                    raise cast_exception

                op = self.builder.inttoptr
                break

            # If we're casting non-pointers of the same bitwidth,
            # just use bitcast

            if cast_from.type.width == cast_to.width:
                op = self.builder.bitcast
                break

            # If we're going from a smaller to a larger bitwidth,
            # we need to use the right instruction

            if cast_from.type.width < cast_to.width:
                if isinstance(cast_from.type, ir.IntType):
                    if isinstance(cast_to, ir.IntType):
                        op = self.builder.zext
                        break
                    if isinstance(cast_to, ir.DoubleType):
                        if cast_from.type.signed:
                            op = self.builder.sitofp
                            break
                        else:
                            op = self.builder.uitofp
                            break
            else:
                cast_exception.msg += ' (data would be truncated)'
                raise cast_exception

            raise cast_exception

        result = op(cast_from, cast_to)
        result.type = cast_to
        return result

    def _codegen_Builtins_convert(self, node):
        '''
        Converts data between primitive value types, such as i8 to i32.
        Checks for signing and bitwidth.
        Conversions from or to pointers are not supported here.
        '''
        convert_from = self._codegen(node.args[0])
        convert_to = self._codegen(node.args[1], False)

        convert_exception = CodegenError(
            f'Converting from type "{convert_from.type.descr()}" to type "{convert_to.descr()}" is not supported',
            node.args[0].position)

        while True:

            # Conversions from/to an object are not allowed

            if convert_from.type.is_obj_ptr() or convert_to.is_obj_ptr():
                explanation = f'\n(Converting from/to object types will be added later.)'
                convert_exception.msg += explanation
                raise convert_exception

            # Convert from/to a pointer is not allowed

            if isinstance(convert_from.type, ir.PointerType) or isinstance(convert_to, ir.PointerType):
                convert_exception.msg += '\n(Converting from or to pointers is not allowed; use "cast" instead)'
                raise convert_exception

            # Convert from float to int is OK, but warn

            if isinstance(convert_from.type, ir.DoubleType):

                if not isinstance(convert_to, ir.IntType):
                    raise convert_exception

                print(
                    CodegenWarning(
                        f'Float to integer conversions ("{convert_from.type.descr()}" to "{convert_to.descr()}") are inherently imprecise',
                        node.args[0].position))

                if convert_from.type.signed:
                    op = self.builder.fptosi
                else:
                    op = self.builder.fptoui
                break

            # Convert from ints

            if isinstance(convert_from.type, ir.IntType):

                # int to float

                if isinstance(convert_to, ir.DoubleType):
                    print(
                        CodegenWarning(
                            f'Integer to float conversions ("{convert_from.type.descr()}" to "{convert_to.descr()}") are inherently imprecise',
                            node.args[0].position))

                    if convert_from.type.signed:
                        op = self.builder.sitofp
                    else:
                        op = self.builder.uitofp
                    break

                # int to int

                if isinstance(convert_to, ir.IntType):

                    # Don't allow mixing signed/unsigned

                    if convert_from.type.signed and not convert_to.signed:
                        raise CodegenError(
                            f'Signed type "{convert_from.type.descr()}" cannot be converted to unsigned type "{convert_to.descr()}"',
                            node.args[0].position)

                    # Don't allow converting to a smaller bitwidth

                    if convert_from.type.width > convert_to.width:
                        raise CodegenError(
                            f'Type "{convert_from.type.descr()}" cannot be converted to type "{convert_to.descr()}" without possible truncation',
                            node.args[0].position)

                    # otherwise, extend bitwidth to convert

                    if convert_from.type.signed:
                        op = self.builder.sext
                    else:
                        op = self.builder.zext
                    break

            raise convert_exception

        result = op(convert_from, convert_to)
        result.type = convert_to
        return result
