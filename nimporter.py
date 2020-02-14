"""
Contains classes to compile Python-Nim Extension modules, import those modules,
and generate exceptions where appropriate.
"""

import sys, os, subprocess, importlib, hashlib, tempfile
from pathlib import Path
from setuptools import Extension

# NOTE(pebaz): https://stackoverflow.com/questions/39660934/error-when-using-importlib-util-to-check-for-library/39661116
from importlib import util


# When True, will always trigger a rebuild of any Nim modules
# Can be set by the importer of this module
IGNORE_CACHE = False


'''
TODO:

[ ] Move hashing/etc. methods to Nimporter class (it's the only one that needs).
[ ] Create one single compile() method for Nimporter.
[x] Create compile_extension() method using compile() with different arguments.
[ ] Create compile_module() method using compile() with different arguments.
[ ] Create compile_library() method using compile() with different arguments.
    [ ] nimble build
        [ ] Libs are installed as dependencies (must have packagename.nim)
        [ ] Bins must be named exactly "main.nim"
    [x] nimble install --accept
[ ] Search for folders with .nimble treat them as one single extension. This is
    the only supported way for nim modules to import each other within a Python
    project. All other Nim files are treated as single extensions.
[ ] Modify the import system to be able to install dependencies from .nimble and
    make it so that Folders themselves can be imported.

[ ] Allow fine-grained control over compiler switches. This can be configured by
    placing a `module-name.cfg` right next to the `.nimble` file. Single modules
    do not have the option of configuring compiler switches.
[ ] Add API to programatically import Nim file with exact compilation switches.
    [ ] Internally calls __compile()
'''



class NimCompilerException(Exception):
    """
    Indicates that the invocation of the Nim compiler has failed.
    Displays the line of code that caused the error as well as the error message
    returned from Nim as a Python Exception.
    """
    def __init__(self, msg):
        try:
            nim_module, error_msg = msg.split(' Error: ')
            nim_module = nim_module.splitlines()[-1]
            mod, (line_col) = nim_module.split('(')
            self.nim_module = Path(mod)
            line, col = line_col.split(',')
            self.line = int(line)
            self.col = int(col[:-1])
            self.error_msg = error_msg
        except Exception as e:
            # Raise the original exception if anything went wrong during parsing
            raise Exception(msg).with_traceback(e.__traceback__) from e
        
    def __str__(self):
        """
        Return the string representation of the given compiler error.
        """
        message = self.error_msg + '\n'
        
        with open(self.nim_module, 'r') as mod:
            line = 0
            for each_line in mod:
                line += 1

                if line == self.line:
                    message += f' -> {each_line}'
                    
                elif line > self.line + 2:
                    break
                
                elif line > self.line - 3:
                    message += f' |  {each_line}'

        message = message.rstrip() + (
            f'\n\nAt {self.nim_module.absolute()} '
            f'{self.line}:{self.col}'
        )
        
        return message


class NimCompiler:
    """
    Nim compiler invoker. Features:
     - Compile Nim files and return any failure messages as Python exceptions.
     - Store hashes of Nim source files to only recompile when module changes.
     - Stores hash in __pycache__ directory to not clutter up file system.
    
    Attributes:
        EXT(str): the extension to use for the importable build artifact.
    """
    EXT = '.pyd' if sys.platform == 'win32' else '.so'
    STDOUT = 1
    STDERR = 2

    @classmethod
    def __compile(cls, nim_args: list, scan_file_handle=STDERR):
        """
        Returns a tuple containing any errors, warnings, or hints from the
        compilation process.
        """
        process = subprocess.run(
            nim_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = process.stdout, process.stderr
        out = out.decode() if out else ''
        err = err.decode() if err else ''

        if scan_file_handle == cls.STDOUT:
            handle = out
        elif scan_file_handle == cls.STDERR:
            handle = err

        lines = handle.splitlines()

        errors = [line for line in lines if 'Error:' in line]
        warnings = [line for line in lines if 'Warning:' in line]
        hints = [line for line in lines if 'Hint:' in line]

        return out, errors, warnings, hints

    @classmethod
    def compile_extension(cls, module_path, module_name_override=None):
        "Compile Nim to C and return Extension pointing to the C source files."

        module_name = module_name_override or module_path.stem
        build_dir = Path(tempfile.mktemp())
        nim_args = (
            'nim cc -c --opt:speed --gc:markAndSweep --app:lib'.split() +
            '-d:release'.split() +
            [f'--nimcache:{build_dir}', f'{module_path}']
        )

        output, errors, warnings, hints = cls.__compile(nim_args)

        for warn in warnings:
            print(warn)

        if errors: raise NimCompilerException(errors[0])

        csources = [str(c) for c in build_dir.iterdir() if c.suffix == '.c']

        return Extension(
            name=module_name,
            sources=csources,
            include_dirs=[str(build_dir)]
        )

    @classmethod
    def compile_library(cls, library_path):
        """
        If there is package.nim, it's definitely a library (nimble install).
        If there is a main.nim, it's definitely a DLL (nimble cc).

        Extensions MUST have a Nim file named "main.nim" at the project root.

        This method has the added bonus of installing any Nimble dependencies.
        """
        # Install global library dependency
        if (library_path / f'{library_path.name}.nim').exists():
            cwd = Path().cwd()
            os.chdir(library_path)
            lib_args = 'nimble install --accept'.split()

            output, errors, warnings, hints = cls.__compile(
                lib_args, scan_file_handle=cls.STDOUT
            )

            os.chdir(cwd)

            for warn in warnings: print(warn)
            
            if errors:
                output, errors, warnings, hints = cls.__compile(
                    nim_args, scan_file_handle=cls.STDOUT
                )

                raise Exception(errors[0].split('Error:')[1].strip())

            return

        # Compile and return Extension
        elif (library_path / 'main.nim').exists():
            module_name = library_path.resolve().name
            module_path = library_path / 'main.nim'
            build_dir = Path(tempfile.mktemp())

            nim_args = (
                'nim cc -c --opt:speed --gc:markAndSweep --app:lib'.split() +
                '-d:release'.split() +
                [f'--nimcache:{build_dir}', f'{module_path}']
            )

            output, errors, warnings, hints = cls.__compile(nim_args)

            for warn in warnings:
                print(warn)

            if errors: raise NimCompilerException(errors[0])

            csources = [str(c) for c in build_dir.iterdir() if c.suffix == '.c']

            return Extension(
                name=module_name,
                sources=csources,
                include_dirs=[str(build_dir)]
            )

        # Improperly formatted extension/library (no main.nim or package.nim)
        else:
            raise Exception(
                f'Error: {library_path} is not formatted properly. '
                f'No main.nim or {library_path.resolve().name}.nim found.'
            )

    @classmethod
    def pycache_dir(cls, module_path):
        """Return the __pycache__ directory as a Path."""
        return module_path.parent / '__pycache__'

    @classmethod
    def hash_filename(cls, module_path):
        """Return the hash filename as a Path."""
        return cls.pycache_dir(module_path) / (module_path.name + '.hash')

    @classmethod
    def is_cache(cls, module_path):
        """
        Return whether or not a __pycache__ directory exists to store hashes and
        build artifacts.
        """
        return cls.pycache_dir(module_path).exists()

    @classmethod
    def is_hashed(cls, module_path):
        """Return whether or not a given Nim file has already been hashed."""
        return cls.hash_filename(module_path).exists()

    @classmethod
    def is_built(cls, module_path):
        """Return whether or not a given Nim file has already been hashed."""
        return cls.build_artifact(module_path).exists()

    @classmethod
    def get_hash(cls, module_path):
        """Returns the bits of the hash for a given Nim module."""
        if not cls.is_hashed(module_path):
            path = module_path.absolute()
            raise Exception(f'Module {path} has not yet been hashed.')
        return cls.hash_filename(module_path).read_bytes()

    @classmethod
    def hash_changed(cls, module_path):
        """
        Return whether or not a given Nim file has changed since last hash. If
        the module has not yet been hashed, returns True.
        """
        if not cls.is_hashed(module_path):
            return True
        return cls.get_hash(module_path) != cls.hash_file(module_path)

    @classmethod
    def hash_file(cls, module_path):
        """
        Returns the hash of the Nim file.
        """
        block_size = 65536
        hasher = hashlib.md5()
        with module_path.open('rb') as file:
            buf = file.read(block_size)
            while len(buf) > 0:
                hasher.update(buf)
                buf = file.read(block_size)
        return hasher.digest()

    @classmethod
    def update_hash(cls, module_path):
        """
        Creates or updates the <mod-name>.nim.hash file within the __pycache__
        directory.
        """
        with cls.hash_filename(module_path).open('wb') as file:
            file.write(cls.hash_file(module_path))

    @classmethod
    def build_artifact(cls, module_path):
        """
        Returns the Path to the built .PYD or .SO. Does not imply it has already
        been built.
        """
        return cls.pycache_dir(module_path) / (module_path.stem + cls.EXT)

    @classmethod
    def compile(cls, module_path, release_mode=False):
        """
        Compiles a given Nim module and returns the path to the built artifact.
        Raises an exception if compilation fails for any reason.
        """
        if not module_path.exists():
            raise Exception(f'{module_path.absolute()} does not exist.')

        build_artifact = cls.build_artifact(module_path)

        nimc_args = (
            # -d:ssl
            'nim c -w:off --opt:speed --threads:on --tlsEmulation:off --app:lib'.split()
            + '--hints:off --parallelBuild:0 --opt:speed'.split()
            + (['-d:release'] if release_mode else [])
            + [f'--out:{build_artifact}', f'{module_path}']
        )
        
        process = subprocess.run(
            nimc_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = process.stdout, process.stderr
        out = out.decode() if out else ''
        err = err.decode() if err else ''

        # Handle any compiler errors
        NIM_COMPILE_ERROR = ' Error: '
        if NIM_COMPILE_ERROR in err:
            raise NimCompilerException(err)
        elif err:
            # NOTE(pebaz): On Windows, Nim spits out the MSVC banner to stderr,
            # causing `err` to not be None. If it built, ignore the error.
            if not build_artifact.exists():
                raise Exception(err)
        elif NIM_COMPILE_ERROR in out:
            raise NimCompilerException(out)
        
        cls.update_hash(module_path)

        return build_artifact

    @classmethod
    def find_nim_std_lib(cls):
        nimexe = Path(shutil.which('nim'))
        if not nimexe:
            return None
        result = nimexe.parent / '../lib'
        if not (result / 'system.nim').exists():
            result = nimexe.resolve().parent / '../lib'
            if not (result / 'system.nim').exists():
                return None
        return result.resolve()


class Nimporter:
    """
    Python module finder purpose-built to find Nim modules on the Python PATH,
    compile them, hide them within the __pycache__ directory with other compiled
    Python files, and then return it as a full Python module.
    This Nimporter can only import Nim modules with procedures exposed via the
    [Nimpy](https://github.com/yglukhov/nimpy) library acting as a bridge.
    """
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        """
        Finds a Nim module and compiles it if it has changed.

        If the Nim module imports other Nim source files and those files change,
        the Nimporter will not be able to detect them and will reuse the cached
        version.

        Args:
            fullname(str): the module to import. Can be 'foo' or 'foo.bar.baz'
            path(list): a list of paths to search first.
            target(str): the target of the import.

        Returns:
            A useable spec object that will be passed to Python during import to
            actually create a Python Module object from the spec.
        """
        parts = fullname.split('.')
        module = parts.pop()
        module_file = f'{module}.nim'
        path = list(path) if path else []  # Ensure that path is always a list
        package = '/'.join(parts)
        search_paths = [
            Path(i)
            for i in (path + sys.path + ['.'])
            if Path(i).is_dir()
        ]

        for search_path in search_paths:
            # NOTE(pebaz): Found an importable/compileable module
            if (search_path / package).glob(module_file):
                module_path = search_path / module_file

                if not module_path.exists():
                    continue

                should_compile = any([
                    IGNORE_CACHE,
                    NimCompiler.hash_changed(module_path),
                    not NimCompiler.is_cache(module_path),
                    not NimCompiler.is_built(module_path)
                ])

                if should_compile:
                    build_artifact = NimCompiler.compile(module_path)
                else:
                    build_artifact = NimCompiler.build_artifact(module_path)
                    
                return util.spec_from_file_location(
                    fullname,
                    location=str(build_artifact.absolute())
                )

    @classmethod
    def import_nim_module(cls, fullname, path:list=None, ignore_cache=False):
        """
        Can be used to explicitly import a module rather than using the `import`
        keyword. Allows the cache to be ignored to solve issues arising from
        caching one module when 10 other imported Nim libraries have changed.

        Example Use:

        >>> # Rather than:
        >>> import foo
        >>> # You can say:
        >>> foo = Nimporter.import_nim_module('foo', ['/some/random/dir'])

        Args:
            fullname(str): the module to import. Can be 'foo' or 'foo.bar.baz'
            path(list): a list of paths to search first.
            ignore_cache(bool): whether or not to use a cached build if found.

        Returns:
            The Python Module object representing the imported PYD or SO file.            
        """
        spec = cls.find_spec(fullname, path)

        # TODO(pebaz): Compile the module anyway if ignore_cache is set.
        if ignore_cache:
            nim_module = Path(spec.origin).parent.parent / (spec.name + '.nim')
            NimCompiler.compile(nim_module)
            sys.path_importer_cache.clear()
            importlib.invalidate_caches()
            if spec.name in sys.modules:
                sys.modules.pop(spec.name)
            spec = cls.find_spec(fullname, path)

        if spec:
            return util.module_from_spec(spec)
        else:
            raise ImportError(f'No module named {fullname}')


def build_nim_extensions():
    """
    Compiles Nim files to C and creates Extensions from them for distribution.
    """



    return dict(
        ext_modules=[]
    )


'''
By putting the Nimpoter at the end of the list of module loaders, it ensures
that Nim code files are imported only if there is not a Python module of the
same name somewhere on the path.
'''
sys.meta_path.append(Nimporter())

# Ensure that Nim files won't be passed up because of other Importers.
sys.path_importer_cache.clear()
importlib.invalidate_caches()
