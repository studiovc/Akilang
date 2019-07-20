from llvmlite import ir, binding
import pickle

import colorama

colorama.init()
CMD = colorama.Fore.YELLOW
REP = colorama.Fore.WHITE

RED = colorama.Fore.RED
XX = colorama.Fore.RESET

GRN = colorama.Fore.GREEN
MAG = colorama.Fore.MAGENTA


class Comment(ir.values.Value):
    def __init__(self, parent, text):
        self.text = text

    def __repr__(self):
        return f"; {self.text}"


ir.instructions.Comment = Comment


def comment(self, txt):
    self._insert(ir.instructions.Comment(self.block, txt))


import time


class Timer:
    def __init__(self):
        self.clock = time.clock

    def __enter__(self):
        self.begin = self.clock()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.end = self.clock()
        self.time = self.end - self.begin


ir.builder.IRBuilder.comment = comment

import ctypes
import os

from core import grammar as AkiParser
from core.codegen import AkiCodeGen
from core.compiler import AkiCompiler, ir
from core.astree import (
    Function,
    Call,
    Prototype,
    ExpressionBlock,
    TopLevel,
    Name,
    VarTypeName,
)
from core.error import AkiBaseErr, ReloadException, QuitException, LocalException
from core.akitypes import AkiTypeMgr, AkiObject
from core import constants


PROMPT = "A>"

USAGE = f"""From the {PROMPT} prompt, type Aki code or enter special commands
preceded by a dot sign:

    {CMD}.about|ab{REP}     : About this program.
    {CMD}.compile|cp{REP}   : Compile current module to executable.
    {CMD}.dump|dp <funcname>{REP}
                  : Dump current module IR to console.
                  : Add <funcname> to dump IR for a function.
    {CMD}.exit|quit|stop|q{REP}
                  : Stop and exit the program.
    {CMD}.export|ex <filename>{REP}
                  : Dump current module to file in LLVM assembler format.
                  : Uses output.ll in current directory as default filename.
    {CMD}.help|.?|.{REP}    : Show this message.
    {CMD}.rerun|..{REP}     : Reload the Python code and restart the REPL. 
    {CMD}.rl[c|r]{REP}      : Reset the interpreting engine and reload the last .aki
                    file loaded in the REPL. Add c to run .cp afterwards.
                    Add r to run main() afterwards.
    {CMD}.reset|~{REP}      : Reset the interpreting engine.
    {CMD}.run|r{REP}        : Run the main() function (if present) in the current
                    module.
    {CMD}.test|t{REP}       : Run unit tests.
    {CMD}.version|ver|v{REP}
                  : Print version information.
    {CMD}.<file>.{REP}      : Load <file>.aki from the src directory.
                    For instance, .l. will load the Conway's Life demo.

These commands are also available directly from the command line, for example: 

    {CMD}aki 2 + 3
    aki test
    aki .myfile.aki{REP}
    
On the command line, the initial dot sign can be replaced with a double dash: 
    
    {CMD}aki --test
    aki --myfile.aki{REP}
"""


class JIT:
    parser = AkiParser

    def __init__(self, typemgr, module_name=None):
        assert typemgr is not None
        self.typemgr = typemgr
        self.types = self.typemgr.types
        self.anon_counter = 0
        self.module_name = module_name
        self.reset(self.typemgr)

    def reset(self, typemgr=None):
        if typemgr is None:
            self.typemgr = AkiTypeMgr()
        else:
            self.typemgr = typemgr
        self.compiler = AkiCompiler()
        self.module = ir.Module(self.module_name)
        self.module.triple = binding.Target.from_default_triple().triple
        self.codegen = AkiCodeGen(self.module, self.typemgr)


def cp(string):
    print(f"{REP}{string}")


class Repl:
    import sys

    VERSION = f"""Python :{sys.version}
LLVM   :{".".join((str(n) for n in binding.llvm_version_info))}
pyaki  :{constants.VERSION}"""

    def __init__(self, typemgr=None):
        self.reset(silent=True, typemgr=typemgr)

    def run(self):
        import shutil

        cols = shutil.get_terminal_size()[0]
        if cols < 80:
            warn = f"\n{RED}Terminal is less than 80 colums wide.\nOutput may not be correctly formatted."
        else:
            warn = ""

        cp(
            f"{GRN}{constants.WELCOME}{warn}\n{REP}Type {CMD}.help{REP} or a command to be interpreted"
        )
        while True:
            try:
                print(f"{REP}{PROMPT}{CMD}", end="")
                text = input()
                self.cmd(text)
            except AkiBaseErr as e:
                print(e)
            except EOFError:
                break

    def cmd(self, text):
        if not text:
            return
        if text[0] == ".":
            if len(text) == 1:
                self.help()
                return
            if text[-1] == ".":
                if len(text) == 2:
                    return self.reload()
                text = text[1:-1]
                self.load_file(text)
                return
            command = text[1:]
        else:
            print(f"{REP}", end="")
            for _ in self.interactive(text):
                print(_)
            return

        cmd_split = command.split(" ")
        cmd_name = cmd_split[0]
        if len(cmd_split) > 1:
            params = cmd_split[1:]
        else:
            params = None

        cmd_func = self.cmds.get(cmd_name)

        if cmd_func is None:
            cp(f'Unrecognized command "{CMD}{command}{REP}"')
            return

        return cmd_func(self, text, params=params)

    def load_file(self, file_to_load, file_path=None, ignore_cache=False):

        if file_path is None:
            file_path = self.paths["source_dir"]

        self.main_cpl.reset()

        filepath = os.path.join(file_path, f"{file_to_load}.aki")

        self.last_file_loaded = file_to_load

        # Attempt to load precomputed module from cache

        if self.settings["ignore_cache"] is True:
            ignore_cache = True

        if not ignore_cache:

            force_recompilation = False

            cache_path = f"{file_path}/__akic__/"
            cache_file = f"{file_to_load}.akic"
            full_cache_path = cache_path + cache_file

            if not os.path.exists(full_cache_path):
                force_recompilation = True
            elif os.path.getmtime(filepath) > os.path.getmtime(full_cache_path):
                force_recompilation = True

            if not force_recompilation:
                try:
                    with open(full_cache_path, "rb") as file:

                        with Timer() as t1:
                            mod_in = pickle.load(file)
                            if mod_in["version"] != constants.VERSION:
                                raise LocalException

                        file_size = os.fstat(file.fileno()).st_size

                        cp(f"Loaded {file_size} bytes from {CMD}{filepath}{REP}")
                        cp(f"  Parse: {t1.time:.3f} sec")

                        ast = mod_in["ast"]
                        text = mod_in["text"]

                        self.main_cpl.codegen.text = text

                        with Timer() as t2:
                            try:
                                self.main_cpl.codegen.eval(ast)
                            except Exception as e:
                                self.main_cpl.reset()
                                raise e

                        cp(f"   Eval: {t2.time:.3f} sec")

                        with Timer() as t3:

                            self.main_cpl.compiler.compile_module(
                                self.main_cpl.module, file_to_load
                            )

                        cp(f"Compile: {t3.time:.3f} sec")
                        cp(f"  Total: {t1.time+t2.time+t3.time:.3f} sec")

                    return
                except LocalException:
                    pass
                except Exception as e:
                    cp(f"Error reading cached file", e)

        # If no cache, or cache failed,
        # load original file and compile from scratch

        try:
            with open(filepath) as file:
                text = file.read()
                file_size = os.fstat(file.fileno()).st_size
        except FileNotFoundError:
            raise AkiBaseErr(
                None, file_to_load, f"File not found: {CMD}{filepath}{REP}"
            )

        with Timer() as t1:
            ast = self.repl_cpl.parser.parse(text)

        cp(f"Loaded {file_size} bytes from {CMD}{filepath}{REP}")
        cp(f"  Parse: {t1.time:.3f} sec")

        # Write cached AST to file

        if not ignore_cache and self.settings["cache_compilation"] == True:

            try:
                if not os.path.exists(cache_path):
                    os.makedirs(cache_path)
                with open(full_cache_path, "wb") as file:
                    output = {"version": constants.VERSION, "ast": ast, "text": text}
                    try:
                        pickle.dump(output, file, 4)
                    except Exception:
                        raise LocalException
            except LocalException:
                cp("Can't write cache file")
                os.remove(cache_path)

        self.main_cpl.codegen.text = text

        with Timer() as t2:

            try:
                self.main_cpl.codegen.eval(ast)
            except Exception as e:
                self.main_cpl.reset()
                raise e

        cp(f"   Eval: {t2.time:.3f} sec")

        with Timer() as t3:

            self.main_cpl.compiler.compile_module(self.main_cpl.module, file_to_load)

        cp(f"Compile: {t3.time:.3f} sec")
        cp(f"  Total: {t1.time+t2.time+t3.time:.3f} sec")

    def interactive(self, text, immediate_mode=False):
        # Immediate mode processes everything in the repl compiler.
        # Nothing is retained.
        # Regular mode only uses the REPL to launch things.
        # But its typemgr is the same as the main repl.

        if immediate_mode:
            main = self.repl_cpl
            main_file = None
            repl_file = None
            main.reset()
        else:
            main = self.main_cpl
            main_file = "main"
            repl_file = "repl"
            self.repl_cpl.reset()

        # Tokenize input

        ast = self.repl_cpl.parser.parse(text)

        # Iterate through tokens

        self.repl_cpl.codegen.text = text
        main.codegen.text = text

        # Iterate through the AST nodes.
        # When we encounter an expression node,
        # we add it to a stack of expression nodes.
        # When we encounter a top-level command,
        # we compile it immediately, and don't add it
        # to the stack.

        ast_stack = []

        for _ in ast:

            if isinstance(_, TopLevel):
                main.codegen.eval([_])
                main.compiler.compile_module(main.module, main_file)
                continue

            ast_stack.append(_)

        # When we come to the end of the node list,
        # we take the expression node stack, turn it into
        # an anonymous function, and execute it.

        if ast_stack:

            res, return_type = self.anonymous_function(
                ast_stack, repl_file, immediate_mode
            )

            yield return_type.format_result(res)

    def anonymous_function(
        self, ast_stack, repl_file, immediate_mode=False, call_name_prefix="_ANONYMOUS_"
    ):

        _ = ast_stack[-1]

        self.repl_cpl.anon_counter += 1

        call_name = f"{call_name_prefix}{self.repl_cpl.anon_counter}"
        proto = Prototype(_.index, call_name, (), None)
        func = Function(_.index, proto, ExpressionBlock(_.index, ast_stack))

        if not immediate_mode:
            self.repl_cpl.codegen.other_modules.append(self.main_cpl.codegen)
            # Note: This copies ALL global variables,
            # whether or not they are constants.
            # This may prove problematic later.
            for k, v in self.main_cpl.codegen.module.globals.items():
                if isinstance(v, ir.GlobalVariable):
                    self.repl_cpl.codegen.module.globals[k] = v

        try:
            self.repl_cpl.codegen.eval([func])
        except AkiBaseErr as e:
            if not immediate_mode:
                self.repl_cpl.codegen.other_modules.pop()
            del self.repl_cpl.module.globals[call_name]
            raise e

        first_result_type = self.repl_cpl.module.globals[call_name].return_value.akitype

        # If the result from the codegen is an object,
        # redo the codegen with an addition to the AST stack
        # that extracts the c_data value.

        if isinstance(first_result_type, AkiObject):
            _ = ast_stack.pop()
            ast_stack = []

            ast_stack.append(
                Call(_.index, "c_data", (Call(_.index, call_name, (), None),), None)
            )

            call_name += "_WRAP"
            proto = Prototype(_.index, call_name, (), None)
            func = Function(_.index, proto, ExpressionBlock(_.index, ast_stack))

            # It's unlikely that this codegen will ever throw an error,
            # but I'm keeping this anyway

            try:
                self.repl_cpl.codegen.eval([func])
            except AkiBaseErr as e:
                del self.repl_cpl.module.globals[call_name]
                raise e

            final_result_type = self.repl_cpl.module.globals[
                call_name
            ].return_value.akitype

        else:
            final_result_type = first_result_type

        # Link the main module into the REPL module.

        if not immediate_mode:
            if self.main_cpl.compiler.mod_ref:
                self.repl_cpl.compiler.backing_mod.link_in(
                    self.main_cpl.compiler.mod_ref, True
                )

        self.repl_cpl.compiler.compile_module(self.repl_cpl.module, repl_file)

        if not immediate_mode:
            self.repl_cpl.codegen.other_modules.pop()

        # Retrieve a pointer to the function to execute
        func_ptr = self.repl_cpl.compiler.get_addr(call_name)

        return_type = first_result_type
        return_type_ctype = final_result_type.c()

        # Get the function signature
        func_signature = [
            _.aki.vartype.aki_type.c()
            for _ in self.repl_cpl.module.globals[call_name].args
        ]

        # Generate a result
        cfunc = ctypes.CFUNCTYPE(return_type_ctype, *[])(func_ptr)
        res = cfunc()

        return res, return_type

    def run_tests(self, *a, **ka):
        print(f"{REP}", end="")
        import unittest

        tests = unittest.defaultTestLoader.discover("tests", "*.py")
        unittest.TextTestRunner().run(tests)

    def quit(self, *a, **ka):
        print(XX)
        raise QuitException

    def reload(self, *a, **ka):
        print(XX)
        raise ReloadException

    def help(self, *a, **ka):
        cp(f"\n{USAGE}")

    def about(self, *a, **ka):
        print(f"\n{GRN}{constants.ABOUT}\n\n{self.VERSION}\n")

    def not_implemented(self, *a, **ka):
        cp(f"{RED}Not implemented yet")

    def version(self, *a, **ka):
        print(f"\n{GRN}{self.VERSION}\n")

    def reset(self, *a, typemgr=None, **ka):
        """
        Reset the REPL and all of its objects.
        """
        defaults = constants.defaults()
        self.paths = defaults["paths"]
        self.settings = {}
        self.settings_data = defaults["settings"]
        for k, v in self.settings_data.items():
            self.settings[k] = v[1]
        if typemgr is not None:
            self.typemgr = typemgr
        else:
            self.typemgr = AkiTypeMgr()
        self.types = self.typemgr.types
        self.main_cpl = JIT(self.typemgr, module_name=".main")
        self.repl_cpl = JIT(self.typemgr, module_name=".repl")
        self.last_file_loaded = None
        if not "silent" in ka:
            cp(f"{RED}Workspace reset")

    def run_main(self, *a, **ka):
        self.cmd("main()")

    def dump(self, *a, params, **ka):
        if params is not None and len(params) > 0:
            function = params[0]
        else:
            function = None

        if function:
            to_print = self.main_cpl.codegen.module.globals.get(function, None)
        else:
            to_print = self.main_cpl.codegen.module

        if not to_print:
            cp("Function not found")
        else:
            cp(str(to_print))

    def reload_file(self, *a, **ka):
        if self.last_file_loaded is None:
            cp("No file history to load")
            return
        self.load_file(self.last_file_loaded)

    cmds = {
        "t": run_tests,
        "q": quit,
        ".": reload,
        "ab": about,
        "about": about,
        "compile": not_implemented,
        "cp": not_implemented,
        "dump": dump,
        "dp": dump,
        "exit": quit,
        "quit": quit,
        "stop": quit,
        "q": quit,
        "export": not_implemented,
        "ex": not_implemented,
        "help": help,
        "?": help,
        "rerun": not_implemented,
        "rl": reload_file,
        "rlc": not_implemented,
        "rlr": not_implemented,
        "reset": reset,
        "~": reset,
        "run": run_main,
        "r": run_main,
        "test": run_tests,
        "version": version,
        "ver": version,
        "v": version,
    }

