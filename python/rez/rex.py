import os
import subprocess
import sys
import posixpath
import ntpath
import string
import re
import UserDict
import inspect
import textwrap

ATTR_REG_STR = r"([_a-z][_a-z0-9]*)([._a-z][_a-z0-9]*)*"

DEFAULT_ENV_SEP_MAP = {'CMAKE_MODULE_PATH': ';'}

EnvExpand = string.Template

class CustomExpand(string.Template):
    delimiter = '!'

# Add support for {attribute.lookups}
CustomExpand.pattern = re.compile(r"""
      (?<![$])(?:                        # delimiter (anything other than $)
      (?P<escaped>a^)                |   # Escape sequence (not used)
      (?P<named>a^)                  |   # a Python identifier (not used)
      {(?P<braced>%(braced)s)}       |   # a braced identifier (with periods), OR...
      (?P<invalid>a^)                    # Other ill-formed delimiter exprs (not used)
    )
    """ % {'braced': ATTR_REG_STR}, re.IGNORECASE | re.VERBOSE)

# # Add support for !{attribute.lookups}
# CustomExpand.pattern = re.compile(r"""
#       %(delim)s(?:                     # delimiter AND...
#       (?P<escaped>%(delim)s)       |   # Escape sequence of repeated delimiter, OR...
#       (?P<named>[_a-z][_a-z0-9]*)  |   # a Python identifier, OR...
#       {(?P<braced>%(braced)s)}     |  # a braced identifier (with periods), OR...
#       (?P<invalid>)                    # Other ill-formed delimiter exprs
#     )
#     """ % {'delim': re.escape(CustomExpand.delimiter),
#            'braced': ATTR_REG_STR},
#     re.IGNORECASE | re.VERBOSE)

class AttrDict(UserDict.UserDict):
    """
    Dictionary for doing attribute-based lookups of objects.
    """
    ATTR_REG = re.compile(ATTR_REG_STR + '$', re.IGNORECASE)

    def __getitem__(self, key):
        parts = key.split('.')
        attrs = []
        # work our way back through the hierarchy of attributes looking for an
        # object stored directly in the dict with that key.
        found = False
        while parts:
            try:
                result = self.data['.'.join(parts)]
                found = True
                break
            except KeyError:
                # pop off each failed attribute and store it for attribute lookup
                attrs.append(parts.pop())
        if not found:
            raise KeyError(key)

        # work our way forward through the attribute hierarchy looking up
        # attributes on the found object
        for attr in attrs:
            try:
                result = getattr(result, attr)
            except AttributeError:
                raise KeyError(key)
        return result

    def __setitem__(self, key, value):
        if not isinstance(key, basestring):
            raise TypeError("key must be a string")
        if not self.ATTR_REG.match(key):
            raise ValueError("key must be of the format 'node.attr1.attr2': %r" % key)
        self.data[key] = value

#===============================================================================
# Commands
#===============================================================================

class Command(object):
    def __init__(self, *args):
        self.args = args

    @property
    def name(self):
        return self.__class__.__name__.lower()

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__,
                           ', '.join(repr(x) for x in self.args))

class Setenv(Command):
    pass

class Unsetenv(Command):
    pass

class Prependenv(Command):
    pass

class Appendenv(Command):
    pass

class Alias(Command):
    pass

class Info(Command):
    pass

class Error(Command):
    pass

# TODO: need to determine name of the subprocess/command/shell command
# class Command(Command):
#     pass

class Comment(Command):
    pass

class Source(Command):
    pass

class CommandRecorder(object):
    """
    Utility class for generating a list of `Command` instances and performing string
    variable expansion on their arguments (For local variables, not for
    environment variables)
    """
    def __init__(self, initial_commands=None):
        self.commands = [] if initial_commands is None else initial_commands
        self._expandfunc = None

    def reset_commands(self):
        self.commands = []

    def get_commands(self):
        return self.commands[:]

#     @staticmethod
#     def get_command_classes():
#         pass

    def _expand(self, value):
        if self._expandfunc:
            if isinstance(value, basestring):
                return self._expandfunc(value)
            if isinstance(value, (list, tuple)):
                return [self._expandfunc(v) for v in value]
        return value

    def setenv(self, key, value):
        self.commands.append(Setenv(key, self._expand(value)))

    def unsetenv(self, key):
        self.commands.append(Unsetenv(key))

    def prependenv(self, key, value):
        self.commands.append(Prependenv(key, self._expand(value)))

    def appendenv(self, key, value):
        self.commands.append(Appendenv(key, self._expand(value)))

    def alias(self, key, value):
        self.commands.append(Alias(key, self._expand(value)))

    def info(self, value):
        self.commands.append(Info(self._expand(value)))

    def error(self, value):
        self.commands.append(Error(self._expand(value)))

    def command(self, value):
        self.commands.append(Command(self._expand(value)))

    def comment(self, value):
        self.commands.append(Comment(self._expand(value)))

    def source(self, value):
        self.commands.append(Source(self._expand(value)))

#===============================================================================
# Interpreters
#===============================================================================

class CommandInterpreter(object):
    def __init__(self, respect_parent_env=False, env_sep_map=None):
        '''
        respect_parent_env : bool
            If True, appendenv and prependenv will respect inherited
            environment variables, otherwise they will override them.
        env_sep_map : dict
            If provided, allows for custom separators for certain environment
            variables.  Should be a map of variable name to path separator.
        '''
        self._respect_parent_env = respect_parent_env
        self._env_sep_map = env_sep_map if env_sep_map is not None else {}
        self._set_env_vars = set([])

    def _reset(self):
        self._set_env_vars = set([])

    def _execute(self, command_list):
        lines = []
        for cmd in command_list:
            func = getattr(self, cmd.name)
            result = func(*cmd.args)
            if cmd.name in ['setenv', 'prependenv', 'appendenv']:
                self._set_env_vars.add(cmd.args[0])
            if result is not None:
                lines.append(result)
        return '\n'.join(lines)

    def _env_sep(self, name):
        return self._env_sep_map.get(name, os.pathsep)

    def setenv(self, key, value):
        raise NotImplementedError

    def setenvs(self, key, *values):
        raise NotImplementedError

    def unsetenv(self, key):
        raise NotImplementedError

    def prependenv(self, key, value):
        raise NotImplementedError

    def appendenv(self, key, value):
        raise NotImplementedError

    def alias(self, key, value):
        raise NotImplementedError

    def info(self, value):
        raise NotImplementedError

    def error(self, value):
        raise NotImplementedError

    def comment(self, value):
        raise NotImplementedError

    def source(self, value):
        raise NotImplementedError

class Shell(CommandInterpreter):
    pass

class SH(Shell):
    def setenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        return 'export %s="%s"' % (key, value)

    def unsetenv(self, key):
        return "unset %s" % (key,)

    def prependenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        if key in self._set_env_vars:
            return 'export {key}="{value}{sep}${key}"'.format(key=key,
                                                              sep=self._env_sep(key),
                                                              value=value)
        if not self._respect_parent_env:
            return self.setenv(key, value)
        #return "[[ {key} ]] && export {key}={value}:${key} || export {key}={value}".format(key=key, value=value)
        return textwrap.dedent('''\
            if [[ ${key} ]]; then
                export {key}="{value}"
            else
                export {key}="{value}{sep}${key}"
            fi'''.format(key=key,
                         sep=self._env_sep(key),
                         value=value))

    def appendenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        if key in self._set_env_vars:
            return 'export {key}="${key}{sep}{value}"'.format(key=key,
                                                              sep=self._env_sep(key),
                                                              value=value)
        if not self._respect_parent_env:
            return self.setenv(key, value)
        #return "[[ {key} ]] && export {key}=${key}:{value} || export {key}={value}".format(key=key, value=value)
        return textwrap.dedent('''\
            if [[ ${key} ]]; then
                export {key}="{value}"
            else
                export {key}="${key}{sep}{value}"
            fi'''.format(key=key,
                         sep=self._env_sep(key),
                         value=value))

    def alias(self, key, value):
        # bash aliases don't export to subshells; so instead define a function,
        # then export that function
        return "{key}() { {value}; };\nexport -f {key};".format(key=key,
                                                                value=value)

    def info(self, value):
        # TODO: handle newlines
        return 'echo "%s"' % value

    def error(self, value):
        # TODO: handle newlines
        return 'echo "%s" 1>&2' % value

    def comment(self, value):
        # TODO: handle newlines
        return "# %s" % value

    def source(self, value):
        return 'source "%s"' % value

class CSH(SH):
    def setenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        return 'setenv %s "%s"' % (key, value)

    def unsetenv(self, key):
        return "unsetenv %s" % (key,)

    def prependenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        if key in self._set_env_vars:
            return 'setenv {key}="{value}{sep}${key}"'.format(key=key,
                                                              sep=self._env_sep(key),
                                                              value=value)
        if not self._respect_parent_env:
            return self.setenv(key, value)
        return textwrap.dedent('''\
            if ( ! $?{key} ) then
                setenv {key} "{value}"
            else
                setenv {key} "{value}{sep}${key}"
            endif'''.format(key=key,
                            sep=self._env_sep(key),
                            value=value))

    def appendenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        if key in self._set_env_vars:
            return 'setenv {key}="${key}{sep}{value}"'.format(key=key,
                                                              sep=self._env_sep(key),
                                                              value=value)
        if not self._respect_parent_env:
            return self.setenv(key, value)
        return textwrap.dedent('''\
            if ( ! $?{key} ) then
                setenv {key} "{value}"
            else
                setenv {key} "${key}{sep}{value}"
            endif'''.format(key=key,
                            sep=self._env_sep(key),
                            value=value))

    def alias(self, key, value):
        return "alias %s '%s';" % (key, value)

class Python(CommandInterpreter):
    '''Execute commands in the current python session'''
    def __init__(self, override_parent_env=True, environ=None):
        CommandInterpreter.__init__(self, override_parent_env)
        self._environ = os.environ if environ is None else environ

    def _expand(self, value):
        return EnvExpand(value).safe_substitute(**self._environ)

    def _get_env_list(self, key):
        return self._environ[key].split(self._env_sep(key))

    def _set_env_list(self, key, values):
        self._environ[key] = self._env_sep(key).join(values)

    def _execute(self, command_list):
        CommandInterpreter._execute(self, command_list)
        return self._environ

    def setenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        self._environ[key] = self._expand(value)

    def unsetenv(self, key):
        self._environ.pop(key)

    def prependenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        value = self._expand(value)
        if key in self._set_env_vars or (self._respect_parent_env and key in self._environ):
            parts = self._get_env_list(key)
            parts.insert(0, value)
            self._set_env_list(key, parts)
        else:
            self._environ[key] = value

    def appendenv(self, key, value):
        if isinstance(value, (list, tuple)):
            value = self._env_sep(key).join(value)
        value = self._expand(value)
        if key in self._set_env_vars or (self._respect_parent_env and key in self._environ):
            parts = self._get_env_list(key)
            parts.append(value)
            self._set_env_list(key, parts)
        else:
            self._environ[key] = value

    def alias(self, key, value):
        pass

    def info(self, value):
        print str(self._expand(value))

    def error(self, value):
        print>>sys.stderr, str(self._expand(value))

    def comment(self, value):
        pass

    def source(self, value):
        pass

class WinShell(Shell):
    # These are variables where windows will construct the value from the value
    # from system + user + volatile environment values (in that order)
    WIN_PATH_VARS = ['PATH', 'LibPath', 'Os2LibPath']

    def __init__(self, set_global=False):
        self.set_global = set_global

    def setenv(self, key, value):
        value = value.replace('/', '\\\\')
        # Will add environment variables to user environment variables -
        # HKCU\\Environment
        # ...but not to process environment variables
#        return 'setx %s "%s"\n' % ( key, value )

        # Will TRY to add environment variables to volatile environment variables -
        # HKCU\\Volatile Environment
        # ...but other programs won't 'notice' the registry change
        # Will also add to process env. vars
#        return ('REG ADD "HKCU\\Volatile Environment" /v %s /t REG_SZ /d %s /f\n' % ( key, quotedValue )  +
#                'set "%s=%s"\n' % ( key, value ))

        # Will add to volatile environment variables -
        # HKCU\\Volatile Environment
        # ...and newly launched programs will detect this
        # Will also add to process env. vars
        if self.set_global:
            # If we have a path variable, make sure we don't include items
            # already in the user or system path, as these items will be
            # duplicated if we do something like:
            #   env.PATH += 'newPath'
            # ...and can lead to exponentially increasing the size of the
            # variable every time we do an append
            # So if an entry is already in the system or user path, since these
            # will proceed the volatile path in precedence anyway, don't add
            # it to the volatile as well
            if key in self.WIN_PATH_VARS:
                sysuser = set(self.system_env(key).split(os.pathsep))
                sysuser.update(self.user_env(key).split(os.pathsep))
                new_value = []
                for val in value.split(os.pathsep):
                    if val not in sysuser and val not in new_value:
                        new_value.append(val)
                volatile_value = os.pathsep.join(new_value)
            else:
                volatile_value = value
            # exclamation marks allow delayed expansion
            quotedValue = subprocess.list2cmdline([volatile_value])
            cmd = 'setenv -v %s %s\n' % (key, quotedValue)
        else:
            cmd = ''
        cmd += 'set %s=%s\n' % (key, value)
        return cmd

    def unsetenv(self, key):
        # env vars are not cleared until restart!
        if self.set_global:
            cmd = 'setenv -v %s -delete\n' % (key,)
        else:
            cmd = ''
        cmd += 'set %s=\n' % (key,)
        return cmd

#     def user_env(self, key):
#         return executable_output(['setenv', '-u', key])
# 
#     def system_env(self, key):
#         return executable_output(['setenv', '-m', key])

shells = { 'bash' : SH,
           'sh'   : SH,
           'tcsh' : CSH,
           'csh'  : CSH,
           '-csh' : CSH, # For some reason, inside of 'screen', ps -o args reports -csh...
           'DOS' : WinShell}

def get_shell_name():
    proc = subprocess.Popen(['ps', '-o', 'args=', '-p', str(os.getppid())],
                            stdout=subprocess.PIPE)
    output = proc.communicate()[0]
    return output.strip().split()[0]

def get_command_interpreter(shell=None):
    if shell is None:
        shell = get_shell_name()
    return shells[os.path.basename(shell)]

def interpret(commands, shell=None, **kwargs):
    """
    Convenience function which acts as a main entry point for interpreting commands
    """
    kwargs.setdefault('env_sep_map', DEFAULT_ENV_SEP_MAP)
    return get_command_interpreter(shell)(**kwargs)._execute(commands)

#===============================================================================
# Path Utils
#===============================================================================

if sys.version_info < (2, 7, 4):
    # TAKEN from os.posixpath in python 2.7
    # Join two paths, normalizing ang eliminating any symbolic links
    # encountered in the second path.
    def _joinrealpath(path, rest, seen):
        from os.path import isabs, sep, curdir, pardir, split, join, islink
        if isabs(rest):
            rest = rest[1:]
            path = sep

        while rest:
            name, _, rest = rest.partition(sep)
            if not name or name == curdir:
                # current dir
                continue
            if name == pardir:
                # parent dir
                if path:
                    path, name = split(path)
                    if name == pardir:
                        path = join(path, pardir, pardir)
                else:
                    path = pardir
                continue
            newpath = join(path, name)
            if not islink(newpath):
                path = newpath
                continue
            # Resolve the symbolic link
            if newpath in seen:
                # Already seen this path
                path = seen[newpath]
                if path is not None:
                    # use cached value
                    continue
                # The symlink is not resolved, so we must have a symlink loop.
                # Return already resolved part + rest of the path unchanged.
                return join(newpath, rest), False
            seen[newpath] = None # not resolved symlink
            path, ok = _joinrealpath(path, os.readlink(newpath), seen)
            if not ok:
                return join(path, rest), False
            seen[newpath] = path # resolved symlink

        return path, True
else:
    from os.path import _joinrealpath

def _abspath(root, value):
    # not all variables are paths: only absolutize if it looks like a relative path
    if root and \
        (value.startswith('./') or \
        ('/' in value and not (posixpath.isabs(value) or ntpath.isabs(value)))):
        value = os.path.join(root, value)
    return value

def _split_env(value):
    return value.split(os.pathsep)

def _join_env(values):
    return os.pathsep.join(values)

def _realpath(value):
    # cannot call os.path.realpath because it always calls os.path.abspath
    # output:
    seen = {}
    newpath, ok = _joinrealpath('', value, seen)
    # only call abspath if a link was resolved:
    if seen:
        return os.path.abspath(newpath)
    return newpath

def _nativepath(path):
    return os.path.join(path.split('/'))

def _ntpath(path):
    return ntpath.sep.join(path.split(posixpath.sep))

def _posixpath(path):
    return posixpath.sep.join(path.split(ntpath.sep))

#===============================================================================
# Environment Classes
#===============================================================================

class EnvironDict(UserDict.DictMixin):
    def __init__(self, command_recorder=None):
        self.command_recorder = command_recorder if command_recorder is not None else CommandRecorder()
        self._var_cache = {}

    def set_command_recorder(self, recorder):
        self.command_recorder = recorder

    def get_command_recorder(self):
        return self.command_recorder

    def __getitem__(self, key):
        if key not in self._var_cache:
            self._var_cache[key] = EnvironmentVariable(key, self)
        return self._var_cache[key]

    def __setitem__(self, key, value):
#         if isinstance(value, EnvironmentVariable) and value.name == key:
#             # makes no sense to set ourselves. most likely a result of:
#             # env.VAR += value
#             return
        self[key].set(value)

class EnvironmentVariable(object):
    '''
    class representing an environment variable

    combined with EnvironDict class, tracks changes to the environment
    '''

    def __init__(self, name, environ_map):
        self._name = name
        self._environ_map = environ_map

#     def __str__(self):
#         return '%s = %s' % (self._name, self.value())
# 
#     def __repr__(self):
#         return '%s(%r)' % (self.__class__.__name__, self._name)
# 
#     def __nonzero__(self):
#         return bool(self.value())

    @property
    def name(self):
        return self._name

    def prepend(self, value):
        self._environ_map.get_command_recorder().prependenv(self.name, value)

    def append(self, value):
        self._environ_map.get_command_recorder().appendenv(self.name, value)

    def set(self, value):
        self._environ_map.get_command_recorder().setenv(self.name, value)

    def unset(self):
        self._environ_map.get_command_recorder().unsetenv(self.name)

#     def setdefault(self, value):
#         '''
#         set value if the variable does not yet exist
#         '''
#         if self:
#             return self.value()
#         else:
#             return self.set(value)
# 
#     def __add__(self, value):
#         '''
#         append `value` to this variable's value.
# 
#         returns a string
#         '''
#         if isinstance(value, EnvironmentVariable):
#             value = value.value()
#         return self.value() + value
# 
#     def __iadd__(self, value):
#         self.prepend(value)
#         return self
# 
#     def __eq__(self, value):
#         if isinstance(value, EnvironmentVariable):
#             value = value.value()
#         return self.value() == value
# 
#     def __ne__(self, value):
#         return not self == value
# 
#     def __div__(self, value):
#         return os.path.join(self.value(), *value.split('/'))
# 
#     def value(self):
#         return self.environ.get(self._name, None)
# 
#     def split(self):
#         # FIXME: value could be None.  should we return empty list or raise an error?
#         value = self.value()
#         if value is not None:
#             return _split_env(value)
#         else:
#             return []

class RoutingDict(dict):
    """
    The RoutingDict is a custom dictionary that brings all of the components of
    rex together into a single dictionary interface, which can act as a namespace
    dictionary for use with the python `exec` statement.

    The class routes key lookups between an `EnvironDict` and a dictionary of
    local variables passed in via `vars`. Keys which are ALL_CAPS will be looked
    up in the `EnvironDict`, and the remainder will be looked up in the `vars` dict.

    The `RoutingDict` is also responsible for providing a `CommandRecorder` to
    the `EnvironDict` and providing a variable expansion function to the
    `CommandRecorder`.  It is also responsible for expanding variables which
    are set directly via `__setitem__`.
    """
    ALL_CAPS = re.compile('[_A-Z][_A-Z0-9]*$')

    def __init__(self, vars=None):
        self.command_recorder = CommandRecorder()
        self.command_recorder._expandfunc = self.expand
        self.environ = EnvironDict(self.command_recorder)
        self.vars = vars if vars is not None else globals()
        self.custom = AttrDict()
        self.custom.data = self.vars # assigning to data directly keeps a live link

        # load commands into environment
        for cmd, obj in inspect.getmembers(self.command_recorder):
            if not cmd.startswith('_') and inspect.ismethod(obj):
                self.vars[cmd] = obj

    def expand(self, value):
        value = CustomExpand(value).safe_substitute(self.custom)
        return value

    def set_command_recorder(self, recorder):
        self.command_recorder = recorder
        self.command_recorder._expandfunc = self.expand
        self.environ.set_command_recorder(recorder)

    def get_command_recorder(self):
        return self.command_recorder

    def __getitem__(self, key):
        if self.ALL_CAPS.match(key):
            return self.environ[key]
        else:
            return self.vars[key]

    def __setitem__(self, key, value):
        if self.ALL_CAPS.match(key):
            self.environ[key] = value
        else:
            if isinstance(value, basestring):
                value = self.expand(value)
            self.vars[key] = value


class MachineInfo(object):
    def __init__(self):
        self._fqdn = None
        self._name = None
        self._domain = None
        self._os = None
        self._os_version = None
        self._arch = None

    def __str__(self):
        return self.name

    def _populate_fqdn(self):
        import socket
        self._fqdn = socket.getfqdn()
        self._name, self._domain = self._fqdn.split('.', 1)

    def _populate_platform(self):
        import platform
        self._os = platform.system()
        self._os_version = platform.version()
        self._arch = platform.machine()

    # provide read-only properties to prevent accidental overwrites and to 
    # lazily lookup values
    @property
    def name(self):
        if self._name is None:
            self._populate_fqdn()
        return self._name

    @property
    def fdqn(self):
        if self._fdqn is None:
            self._populate_fqdn()
        return self._fdqn

    @property
    def domain(self):
        if self._domain is None:
            self._populate_fqdn()
        return self._domain

    @property
    def os(self):
        if self._os is None:
            self._populate_platform()
        return self._os

    @property
    def os_version(self):
        if self._os_version is None:
            self._populate_platform()
        return self._os_version

    @property
    def arch(self):
        if self._arch is None:
            self._populate_platform()
        return self._arch

def _test():
    print "-"  * 40
    print "environ dictionary + sh executor"
    print "-"  * 40
    d = EnvironDict()
    d['SIMPLESET'] = 'aaaaa'
    d['APPEND'].append('bbbbb')
    d['EXPAND'] = '$SIMPLESET/cccc'
    d['SIMPLESET'].prepend('dddd')
    d['SPECIAL'] = 'eeee'
    d['SPECIAL'].append('ffff')
    sh = SH(env_sep_map={'SPECIAL' : "';'"})
    print sh._execute(d.commands._commands)

    print "-"  * 40
    print "exec + routing dictionary + reused sh executor"
    print "-"  * 40
    sh._reset()
    code = '''
localvar = 'AAAA'
FOO = 'bar'
SIMPLESET = 'aaaaa-!localvar'
APPEND.append('bbbbb/!custom1')
EXPAND = '$SIMPLESET/cccc-!custom2'
SIMPLESET.prepend('dddd')
SPECIAL = 'eeee'
SPECIAL.append('${FOO}/ffff')
comment("testing commands:")
info("the value of localvar is !localvar")
error("oh noes")
'''
    g = RoutingDict()
    g['custom1'] = 'one'
    g['custom2'] = 'two'
    exec code in g

    print sh._execute(g.commands)

    print "-"  * 40
    print "re-execute record with python executor"
    print "-"  * 40

    import pprint
    environ = {}
    py = Python(environ=environ)
    pprint.pprint(py._execute(g.commands._commands))

    print "-"  * 40
    print "exec + routing dictionary + attr dictionary"
    print "-"  * 40
    sh._reset()
    code = '''
info("this is the value of !{thing.name} and !{thing.bar}")
'''

    class Foo(object):
        bar = 'value'

    custom = AttrDict({'thing.name': 'name',
                       'thing': Foo})
    g = RoutingDict(custom=custom)
    exec code in g

    print sh._execute(g.commands)

    code = '''
short_version = '!V1.!V2'
if package_present('fedora') or package_present('Darwin'):
  APP = '/apps/!NAME/%short_version'
elif not package_present('ubuntu'):
  APP = 'C:/apps/!NAME/%short_version'
if package_present('python-3') and not REALLY:
  REALLY = 'yeah'
PATH.append('$APP/bin')
if package_present('Linux') and building():
  LD_LIBRARY_PATH.append('!ROOT/lib')
alias('!NAME-!VERSION', '$APP/bin/!NAME')
shell('startserver !NAME')'''
    custom = dict(V1='1', V2='2',
                  VERSION='1.2.3',
                  NAME='my_test_app',
                  ROOT='/path/to/root')
