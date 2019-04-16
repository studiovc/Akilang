from core.error import AkiSyntaxErr
from llvmlite import ir
from typing import Optional


class ASTNode:
    """
    Base type for all AST nodes, with helper functions.
    """

    def __init__(self, p):
        self.p = p
        self.child = None
        self.lineno = p.lineno
        self.index = p.index

    def __eq__(self, other):
        raise NotImplementedError

    def flatten(self):
        return [self.__class__.__name__, "flatten unimplemented"]


class Expression(ASTNode):
    """
    Base type for all expressions.
    """

    pass


class Keyword(ASTNode):
    """
    Base type for keywords.
    """

    pass


class TopLevel(ASTNode):
    """
    Mixin type for top-level AST nodes.
    """

    pass


class VarTypeNode(Expression):
    name: Optional[str] = None


class VarTypeName(VarTypeNode):
    def __init__(self, p, name: str):
        super().__init__(p)
        self.name = name

    def __eq__(self, other):
        return self.name == other.name

    def flatten(self):
        return [self.__class__.__name__, self.name]


class VarTypePtr(VarTypeNode):
    def __init__(self, p, pointee: VarTypeNode):
        super().__init__(p)
        self.pointee = pointee
        self.name = f"ptr {pointee.name}"

    def __eq__(self, other):
        return self.pointee == other.pointee

    def flatten(self):
        return [self.__class__.__name__, self.pointee.flatten()]


class VarTypeFunc(VarTypeNode):
    def __init__(self, p, arguments, return_type: VarTypeNode):
        super().__init__(p)
        self.arguments = arguments
        self.return_type = return_type

    def __eq__(self, other):
        return (
            self.arguments == other.arguments and self.return_type == other.return_type
        )

    def flatten(self):
        return [
            self.__class__.__name__,
            self.arguments.flatten() if self.arguments else [],
            self.return_type.flatten() if self.return_type else None,
        ]


class Name(Expression):
    """
    Variable reference.
    """

    def __init__(self, p, name, val=None, vartype=None):
        super().__init__(p)
        self.name = name
        self.val = val
        # `val` is only used in variable assignment form
        self.vartype = vartype
 
    def __eq__(self, other):
        return self.name == other.name

    def flatten(self):
        return [
            self.__class__.__name__,
            self.name,
            self.val.flatten() if self.val else None,
            self.vartype.flatten() if self.vartype else None,
        ]


class VarList(Expression):
    """
    `var` declaration with one or more variables.
    """

    def __init__(self, p, vars):
        super().__init__(p)
        self.vars = vars

    def __eq__(self, other):
        return self.vars == other.vars

    def flatten(self):
        return [
            self.__class__.__name__,
            [_.flatten() for _ in self.vars] if self.vars else [],
        ]


class Argument(ASTNode):
    """
    Function argument, with optional type declaration.
    """

    def __init__(self, p, name, vartype=None, default_value=None):
        super().__init__(p)
        self.name = name
        self.vartype = vartype
        self.default_value = default_value

    def __eq__(self, other):
        return self.name == other.name and self.vartype == other.vartype

    def flatten(self):
        return [
            self.__class__.__name__,
            self.name,
            self.vartype.flatten(),
            self.default_value.flatten() if self.default_value else None,
        ]


class Constant(Expression):
    """
    LLVM constant value.
    """

    def __init__(self, p, val, vartype):
        super().__init__(p)
        self.val = val
        self.vartype = vartype

    def __eq__(self, other):
        return self.val == other.val and self.vartype == other.vartype

    def flatten(self):
        return [self.__class__.__name__, self.val, self.vartype.flatten()]


class String(Expression):
    """
    String constant.
    """

    def __init__(self, p, val, vartype):
        super().__init__(p)
        self.val = val
        self.vartype = vartype
        self.name = val

    def __eq__(self, other):
        return self.val == other.val

    def flatten(self):
        return [self.__class__.__name__, self.val]


class UnOp(Expression):
    """
    Unary operator expression.
    """

    def __init__(self, p, op, lhs):
        super().__init__(p)
        self.op = op
        self.lhs = lhs

    def __eq__(self, other):
        return self.op == other.op and self.lhs == other.lhs

    def flatten(self):
        return [self.__class__.__name__, self.op, self.lhs.flatten()]


class RefExpr(Expression):
    """
    Reference expression (obtaining a pointer to an object)
    """

    def __init__(self, p, ref):
        super().__init__(p)
        self.ref = ref

    def __eq__(self, other):
        return self.ref == other.ref

    def flatten(self):
        return [self.__class__.__name__, self.ref.flatten()]


class DerefExpr(RefExpr):
    pass


class BinOp(Expression):
    """
    Binary operator expression.
    """

    def __init__(self, p, op, lhs, rhs):
        super().__init__(p)
        self.op = op
        self.lhs = lhs
        self.rhs = rhs

    def __eq__(self, other):
        return self.op == other.op and self.lhs == other.lhs and self.rhs == other.rhs

    def flatten(self):
        return [
            self.__class__.__name__,
            self.op,
            self.lhs.flatten(),
            self.rhs.flatten(),
        ]


class Assignment(BinOp):
    pass


class BinOpComparison(BinOp):
    pass


class IfExpr(ASTNode):
    def __init__(self, p, if_expr, then_expr, else_expr=None):
        super().__init__(p)
        self.if_expr = if_expr
        self.then_expr = then_expr
        self.else_expr = else_expr

    def __eq__(self, other):
        raise NotImplementedError

    def flatten(self):
        return [
            self.__class__.__name__,
            self.if_expr.flatten(),
            self.then_expr.flatten(),
            self.else_expr.flatten() if self.else_expr else None,
        ]


class WhenExpr(IfExpr):
    pass


class Prototype(ASTNode):
    """
    Function prototype.
    """

    def __init__(
        self,
        p,
        name: str,
        arguments: list,
        return_type: VarTypeNode,
        is_declaration=False,
    ):
        super().__init__(p)
        self.name = name
        self.arguments = arguments
        self.return_type = return_type
        self.is_declaration = is_declaration

    def __eq__(self, other):
        return (
            self.arguments == other.arguments and self.return_type == other.return_type
        )

    def flatten(self):
        return [
            self.__class__.__name__, self.name,
            [_.flatten() for _ in self.arguments] if self.arguments else [],
            self.return_type.flatten() if self.return_type else None,
        ]


class Function(TopLevel, ASTNode):
    """
    Function body.
    """

    def __init__(self, p, prototype, body):
        super().__init__(p)
        self.prototype = prototype
        self.body = body

    def flatten(self):
        return [
            self.__class__.__name__,
            self.prototype.flatten(),
            [_.flatten() for _ in self.body.body],
        ]

class External(Function):
    pass

class Call(Expression, Prototype):
    """
    Function call.
    Re-uses Prototype since it has the same basic structure.
    Arguments contains a list of Expression-class ASTs.
    """

    pass


class ExpressionBlock(Expression):
    """
    {}-delimeted set of expressions, stored as a list in `body`.
    """

    def __init__(self, p, body):
        super().__init__(p)
        self.body = body

    def flatten(self):
        return [self.__class__.__name__, [_.flatten() for _ in self.body]]


class LLVMNode(Expression):
    """
    Repackages an LLVM op as if it were an unprocessed AST node.
    You should use this if:
    a) you have an op that has been produced by a previous codegen action
    b) you're going to codegen a synthetic AST node using that result as a parameter        
    """

    def __init__(self, node, vartype, llvm_node):
        super().__init__(node.p)

        # Aki node, for position information
        self.node = node

        # Vartype (an AST vartype node provided by the caller)
        # This can also also be an .akitype node
        # so it can just be copied from the last instruction

        self.vartype = vartype

        # LLVM node
        # This MUST have .akitype and .akinode data

        assert isinstance(self.llvm_node, ir.Instruction)

        self.llvm_node = llvm_node
        assert self.llvm_node.akinode
        assert self.llvm_node.akitype

        # Name (optional)
        self.name = None


class LoopExpr(Expression):
    def __init__(self, p, conditions, body):
        super().__init__(p)
        self.conditions = conditions
        self.body = body

    def flatten(self):
        return [
            self.__class__.__name__,
            [_.flatten() for _ in self.conditions],
            self.body.flatten(),
        ]


class Break(Expression):
    def __init__(self, p):
        super().__init__(p)

    def flatten(self):
        return [self.__class__.__name__]


class WithExpr(Expression):
    def __init__(self, p, varlist: VarList, body: ExpressionBlock):
        super().__init__(p)
        self.varlist = varlist
        self.body = body

    def flatten(self):
        return [
            self.__class__.__name__,
            [_.flatten() for _ in self.varlist.vars],
            self.body.flatten(),
        ]

class ChainExpr(Expression):
    def __init__(self, p, exprchain: list):
        super().__init__(p)
        self.exprchain = exprchain

    def flatten(self):
        return [self.__class__.__name__, [_.flatten() for _ in self.exprchain]]

# class Accessor:
# object to access, and one or more accessors with dimensions