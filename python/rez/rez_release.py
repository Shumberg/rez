"""
rez-release

A tool for releasing rez - compatible projects centrally
"""

import sys
import os
import os.path
import shutil
import inspect
import time
import subprocess
import smtplib
import textwrap
from email.mime.text import MIMEText

from rez.rez_util import remove_write_perms, copytree, get_epoch_time, safe_chmod
from rez.rez_metafile import *
import rez.public_enums as enums
import rez.builds as builds
import versions

##############################################################################
# Globals
##############################################################################

_release_classes = []

##############################################################################
# Exceptions
##############################################################################

class RezReleaseError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)

class RezReleaseUnsupportedMode(RezReleaseError):
    """
    Raise this error during initialization of a RezReleaseMode sub-class to indicate
    that the mode is unsupported in the given context
    """
    pass

##############################################################################
# Constants
##############################################################################

REZ_RELEASE_PATH_ENV_VAR = "REZ_RELEASE_PACKAGES_PATH"
EDITOR_ENV_VAR = "REZ_RELEASE_EDITOR"
RELEASE_COMMIT_FILE = "rez-release-commit.tmp"


##############################################################################
# Public Functions
##############################################################################

def register_release_mode(cls):
    """
    Register a subclass of RezReleaseMode for performing a custom release procedure.
    """
    import re
    assert inspect.isclass(cls) and issubclass(cls, RezReleaseMode), \
        "Provided class is not a subclass of RezReleaseMode"
    assert hasattr(cls, 'name'), "Mode must have a name attribute"
    assert re.match("[a-zA-Z][a-zA-Z0-9_]+$", cls.name),\
        "Mode name '%s' must begin with a letter and contain no spaces" % cls.name
    assert cls.name not in list_release_modes(), \
        "Mode '%s' has already been registered" % cls.name
    # put new entries at the front
    _release_classes.insert(0, (cls.name, cls))

def list_release_modes():
    return [name for (name, cls) in _release_classes]

def get_release_mode(path):
    """
    get the best release mode given the root path
    """
    for name, cls in _release_classes:
        if cls.is_valid_root(path):
            return cls(path)

def list_available_release_modes(path):
    """
    List release modes that work with the given path.

    Note that this does not filter release modes which are broken -- ie. a module
    fails to import, the VCS binary is unavailable, etc -- so that these issues are
    not masked and the user can get a chance to fix them. For example, if a
    root directory contains a .svn directory, 'svn' will be a valid release mode
    even if the pysvn module is not avaialble.
    """
    return [name for name, cls in _release_classes if cls.is_valid_root(path)]

def release_from_path(path, commit_message, njobs, build_time, allow_not_latest,
                      mode='svn'):
    """
    release a package from the given path on disk, copying to the relevant tag,
    and performing a fresh build before installing it centrally.

    path:
            filepath containing the project to be released
    commit_message:
            None, or message string to write to svn, along with changelog.
            If 'commit_message' None, the user will be prompted for input using the
            editor specified by $REZ_RELEASE_EDITOR.
    njobs:
            number of threads to build with; passed to make via -j flag
    build_time:
            epoch time to build at. If 0, use current time
    allow_not_latest:
            if True, allows for releasing a tag that is not > the latest tag version
    """
    cls = dict(_release_classes)[mode]
    try:
        rel = cls(path)
    except RezReleaseUnsupportedMode as err:
        print err
        return
    rel.release(commit_message, njobs, build_time, allow_not_latest)


##############################################################################
# Utilities
##############################################################################

def _format_bash_command(args):
    def quote(arg):
        if ' ' in arg:
            return "'%s'" % arg
        return arg
    cmd = ' '.join([quote(arg) for arg in args])
    return textwrap.dedent("""
        echo
        echo rez-build: calling \\'%(cmd)s\\'
        %(cmd)s
        if [ $? -ne 0 ]; then
            exit 1 ;
        fi
        """ % {'cmd' : cmd})

def unversioned(pkgname):
    return pkgname.split('-')[0]

def _expand_path(path):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))

def send_release_email(subject, body):
    from_ = os.getenv("REZ_RELEASE_EMAIL_FROM", "rez")
    to_ = os.getenv("REZ_RELEASE_EMAIL_TO")
    if not to_:
        return
    recipients = to_.replace(':', ' ').replace(';', ' ').replace(',', ' ')
    recipients = recipients.strip().split()
    if not recipients:
        return

    print
    print("---------------------------------------------------------")
    print("rez-release: sending notification emails...")
    print("---------------------------------------------------------")
    print
    print "sending to:\n%s" % str('\n').join(recipients)

    smtphost = os.getenv("REZ_RELEASE_EMAIL_SMTP_HOST", "localhost")
    smtpport = os.getenv("REZ_RELEASE_EMAIL_SMTP_PORT")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_
    msg["To"] = str(',').join(recipients)

    try:
        s = smtplib.SMTP(smtphost, smtpport)
        s.sendmail(from_, recipients, msg.as_string())
        print 'email(s) sent.'
    except Exception as e:
        print >> sys.stderr, "Emailing failed: %s" % str(e)

##############################################################################
# Implementation Classes
##############################################################################

class RezReleaseMode(object):
    '''
    Base class for all release modes.

    A release mode typically corresponds to a particular version control system
    (VCS), such as svn, git, or mercurial (hg).

    The base implementation allows for release without the use of any version
    control system.

    To implement a new mode, start by creating a subclass overrides to the
    high level methods:
            - validate_repostate
            - create_release_tag
            - get_tags
            - get_tag_meta_str
            - copy_source

    If you need more control, you can also override the lower level methods that
    correspond to the release phases:
            - pre_build
            - build
            - install
            - post_install
    '''
    name = 'base'

    def __init__(self, path):
        self.release_install = False
        self.root_dir = _expand_path(path)

        self.changelog = None
        self.now_epoch = get_epoch_time()

        # variables filled out in pre_build()
        self.metadata = None
        self.family_install_dir = None
        self.package_uuid_exists = None
        self.editor = None

        # for cached property: False indicates it has not been cached
        # (since it may be None after caching)
        self._last_tagged_version = False

        # filled in release()
        self.commit_message = None
        self.njobs = 1
        self.build_time = None
        self.allow_not_latest = False

        self.base_build_dir = None

    @classmethod
    def is_valid_root(cls, path):
        """
        Return True if this release mode works with the given root path
        """
        return True

    def release(self, commit_message, njobs, build_time, allow_not_latest):
        '''
        Main entry point for executing the release
        '''
        self.commit_message = commit_message
        self.njobs = njobs
        # any packages newer than this time will be ignored. This serves two purposes:
        # 1) It stops inconsistent builds due to new packages getting released during a build;
        # 2) It gives us the ability to reproduce a build that happened in the past, ie we can make
        # it resolve the way that it did, rather than the way it might today
        self.build_time = build_time
        if str(self.build_time) == "0":
            self.build_time = self.now_epoch

        self.allow_not_latest = allow_not_latest

        self.init(central_release=True)
        self.pre_build()
        self.build()
        self.install()
        self.post_install()

    def get_metadata(self):
        '''
        return a ConfigMetadata instance for this project's package.yaml file.
        '''
        # check for ./package.yaml
        yaml_path = os.path.join(self.root_dir, "package.yaml")
        if not os.access(yaml_path, os.F_OK):
            raise RezReleaseError(yaml_path + " not found")

        # load the package metadata
        metadata = ConfigMetadata(yaml_path + "")
        if (not metadata.version):
            raise RezReleaseError(yaml_path + " does not specify a version")
        try:
            self.this_version = versions.Version(metadata.version)
        except versions.VersionError:
            raise RezReleaseError(yaml_path + " contains illegal version number")

        # metadata must have name
        if not metadata.name:
            raise RezReleaseError(yaml_path + " is missing name")

        # metadata must have uuid
        if not metadata.uuid:
            raise RezReleaseError(yaml_path + " is missing uuid")

        # .metadata must have description
        if not metadata.description:
            raise RezReleaseError(yaml_path + " is missing a description")

        # metadata must have authors
        if not metadata.authors:
            raise RezReleaseError(yaml_path + " is missing authors")

        return metadata

    # utilities  ---------
    def _write_changelog(self, changelog_file):
        if self.changelog:
            if self.commit_message:
                self.commit_message += '\n' + self.changelog
            else:
                # prompt for tag comment, automatically setting to the change-log
                self.commit_message = "\n\n" + self.changelog

            # write the changelog to file, so that rez-build can install it as metadata
            with open(changelog_file, 'w') as chlogf:
                chlogf.write(self.changelog)
        else:
            print "no changelog. not writing %s" % os.path.basename(changelog_file)

    def _get_commit_message(self):
        '''
        Prompt user for a commit message using the editor specified by
        $REZ_RELEASE_EDITOR.

        The starting value of the editor will be the message passed on the
        command-line, if given.
        '''

        tmpf = os.path.join(self.base_build_dir, RELEASE_COMMIT_FILE)
        f = open(tmpf, 'w')
        f.write(self.commit_message)
        f.close()

        try:
            returncode = subprocess.call(self.editor + ' ' + tmpf, shell=True)
            if returncode == 0:
                print "Got commit message"
                # if commit file was unchanged, then give a chance to abort the release
                new_commit_message = open(tmpf).read()
                if (new_commit_message == self.commit_message):
                    try:
                        reply = raw_input("Commit message unchanged - (a)bort or (c)ontinue? ")
                        if reply != 'c':
                            sys.exit(1)
                    except EOFError:
                        # raw_input raises EOFError on Ctl-D (Unix) and Ctl-Z+Return (Windows)
                        sys.exit(1)
                self.commit_message = new_commit_message
            else:
                raise RezReleaseError("Error getting commit message")
        finally:
            # always remove the temp file
            os.remove(tmpf)

    def check_uuid(self, package_uuid_file):
        '''
        check uuid against central uuid for this package family, to ensure that
        we are not releasing over the top of a totally different package due to
        naming clash
        '''
        try:
            existing_uuid = open(package_uuid_file).read().strip()
        except Exception:
            package_uuid_exists = False
            existing_uuid = self.metadata.uuid
        else:
            package_uuid_exists = True

        if existing_uuid != self.metadata.uuid:
            raise RezReleaseError("the uuid in '" + package_uuid_file +
                                  "' does not match this package's uuid - you may have a package "
                                  "name clash. All package names must be unique.")
        return package_uuid_exists

    def write_time_metafile(self):
        # the very last thing we do is write out the current date-time to a metafile. This is
        # used by rez to specify when a package 'officially' comes into existence.
        time_metafile = os.path.join(self.version_install_dir,
                                     '.metadata', 'release_time.txt')
        with open(time_metafile, 'w') as f:
            f.write(str(get_epoch_time()) + '\n')

    def send_email(self):
        usr = os.getenv("USER", "unknown.user")
        pkgname = "%s-%s" % (self.metadata.name, str(self.this_version))
        subject = "[rez] [release] %s released %s" % (usr, pkgname)
        if len(self.variants) > 1:
            subject += " (%d variants)" % len(self.variants)
        send_release_email(subject, self.commit_message)

    def check_installed_variant(self, instpath):
        for root, dirs, files in os.walk(instpath):
            has_py = False
            for name in files:
                path = os.path.join(root, name)
                # remove any .pyc files that may have been spawned
                if name.endswith('.pyc'):
                    os.remove(path)
                elif not name == '.metadata':
                    if name.endswith('.py'):
                        has_py = True
                    # Remove write permissions from all installed files.
                    remove_write_perms(path)
            # Remove write permissions on dirs that contain py files
            if has_py:
                remove_write_perms(root)

    # VCS and tagging ---------
    def get_url(self):
        '''
        Return a string for the remote url that best identifies the source of
        this VCS repository.
        '''
        return

    def create_release_tag(self):
        '''
        On release, it is customary for a VCS to generate a tag
        '''
        pass

    def get_tags(self):
        '''
        Return a list of tags for this VCS
        '''
        return []

    def get_tag_meta_str(self):
        '''
        Return a tag identifier string for this VCS.
        Could be a url, revision, hash, etc.
        Cannot contain spaces, dashes, or newlines.
        '''
        return

    @property
    def last_tagged_version(self):
        '''
        Cached property for the last tagged version.  None if there are no tags.
        '''
        if self._last_tagged_version is False:
            # False means the value has not been cached yet
            self._last_tagged_version = self.get_last_tagged_version()
        return self._last_tagged_version

    def get_last_tagged_version(self):
        '''
        Find the latest tag returned by self.get_tags() or None if there are
        no tags.
        '''
        latest_ver = versions.Version("0")

        found_tag = False
        for tag in self.get_tags():
            try:
                ver = versions.Version(tag)
            except Exception:
                continue
            if ver > latest_ver:
                latest_ver = ver
                found_tag = True

        if not found_tag:
            return
        return latest_ver

    def validate_version(self):
        '''
        validate the version being released, by ensuring it is greater than the
        latest existing tag, as provided by self.last_tagged_version property.

        Ignored if allow_not_latest is True.
        '''
        if self.allow_not_latest:
            return

        if self.last_tagged_version is None:
            return

        last_tag_str = str(self.last_tagged_version)
        if last_tag_str[0] == 'v':
            # old style
            return

        # FIXME: is the tag put under version control really our most reliable source
        # for previous released versions? Can't we query the versions of our package
        # on $REZ_RELEASE_PACKAGES_PATH?
        if self.this_version <= self.last_tagged_version:
            raise RezReleaseError("cannot release: current version '" + self.metadata.version +
                                  "' is not greater than the latest tag '" + last_tag_str +
                                  "'. Version up or pass --allow-not-latest.")

    def validate_repostate(self):
        '''
        ensure that the VCS working copy is up-to-date
        '''
        pass

    def copy_source(self, build_dir):
        '''
        Copy the source to the build directory.

        This is particularly useful for revision control systems, which can
        export a clean unmodified copy
        '''
        root_build_dir = os.path.dirname(self.base_build_dir)

        def ignore(src, names):
            '''
            returns a list of names to ignore, given the current list
            '''
            if src == root_build_dir:
                return names
            return [x for x in names if x.startswith('.')]

        print "copying directory", self.root_dir
        copytree(self.root_dir, build_dir, symlinks=True, hardlinks=True,
                 ignore=ignore)

    def get_changelog(self):
        '''
        get the changelog text since the last release
        '''
        # during release, changelog.txt must always exists, because RezBuild
        # will blindly try to install it
        return 'not under version control'

    # building ---------
    def _get_cmake_args(self, build_system, build_target):
        BUILD_SYSTEMS = {'eclipse': "Eclipse CDT4 - Unix Makefiles",
                         'codeblocks': "CodeBlocks - Unix Makefiles",
                         'make': "Unix Makefiles",
                         'xcode': "Xcode"}

        cmake_arguments = ["-DCMAKE_SKIP_RPATH=1",
                           "-DCMAKE_MODULE_PATH=$CMAKE_MODULE_PATH"]

        # Fetch the initial cache if it's defined
        if 'CMAKE_INITIAL_CACHE' in os.environ:
            cmake_arguments.extend(["-C", "$CMAKE_INITIAL_CACHE"])

        cmake_arguments.extend(["-G", BUILD_SYSTEMS[build_system]])

        cmake_arguments.append("-DCMAKE_BUILD_TYPE=%s" % build_target)

        if self.release_install:
# 			if os.environ.get('REZ_IN_REZ_RELEASE') != "1":
# 				result = raw_input("You are attempting to install centrally outside "
# 								   "of rez-release: do you really want to do this (y/n)? ")
# 			if result != "y":
# 				sys.exit(1)
            cmake_arguments.append("-DCENTRAL=1")

        return cmake_arguments

    def _build_variant(self, variant_num, build_system='eclipse',
                       build_target='Release', mode=enums.RESOLVE_MODE_LATEST,
                       no_assume_dt=False, do_build=True, cmake_args=(),
                       retain_cmake_cache=False, make_args=(), make_clean=True):
        '''
        Do the actual build of the variant, by resolving an environment and calling
        cmake/make.
        '''
        variant = self.variants[variant_num]
        if variant:
            build_dir = os.path.join(self.base_build_dir, str(variant_num))
            build_dir_symlink = os.path.join(self.base_build_dir, '_'.join(variant))
            variant_subdir = os.path.join(*variant)
        else:
            build_dir = self.base_build_dir

        cmake_args = self._get_cmake_args(build_system, build_target)
        cmake_args.extend(cmake_args)
        make_args = list(make_args)

# 		# build it

        variant_str = ' '.join(variant)
        if variant:
            print
            print "---------------------------------------------------------"
            print "rez-build: building for variant '%s'" % variant_str
            print "---------------------------------------------------------"

        if not os.path.exists(build_dir):
            os.makedirs(build_dir)
        if variant and not os.path.islink(build_dir_symlink):
            os.symlink(os.path.basename(build_dir), build_dir_symlink)

        src_file = os.path.join(build_dir, 'build-env.sh')
        env_bake_file = os.path.join(build_dir, 'build-env.context')
        actual_bake = os.path.join(build_dir, 'build-env.actual')
        dot_file = os.path.join(build_dir, 'build-env.context.dot')
        changelog_file = os.path.join(build_dir, 'changelog.txt')

        # FIXME: move this to SVN class
# 		# allow the svn pre-commit hook to identify the build directory as such
#		build_dir_id = os.path.join(build_dir_base, ".rez-build")
# 		with open(build_dir_id, 'w') as f:
# 			f.write('')
#
        if self.release_install:
            vcs_metadata = self.get_tag_meta_str()
        else:
            # TODO: default mode?
            vcs_metadata = "(NONE)"

        # FIXME: use yaml for info.txt?
        meta_file = os.path.join(build_dir, 'info.txt')
        # store build metadata
        with open(meta_file, 'w') as f:
            import getpass
            f.write("ACTUAL_BUILD_TIME: %d" % self.now_epoch)
            f.write("BUILD_TIME: %s" % self.build_time)
            f.write("USER: %s" % getpass.getuser())
            # FIXME: change entry SVN to VCS
            f.write("SVN: %s" % vcs_metadata)

        self._write_changelog(changelog_file)

        # attempt to resolve env for this variant
        print
        print "rez-build: invoking rez-config with args:"
        # print "$opts.no_archive $opts.ignore_blacklist $opts.no_assume_dt --time=$opts.time"
        print "requested packages: %s" % (', '.join(self.requires + variant))
        print "package search paths: %s" % (os.environ['REZ_PACKAGES_PATH'])

        try:
            import rez.rez_config
            resolver = rez.rez_config.Resolver(mode,
                                               time_epoch=self.build_time,
                                               assume_dt=not no_assume_dt)
            result = resolver.guarded_resolve((self.requires + variant + ['cmake=l']),
                                              dot_file)
            # FIXME: raise error here, or use unguarded resolve
            pkg_ress, commands, dot_graph, num_fails = result

            import rez.rex
            # TODO: support other shells
            script = rez.rex.interpret(commands, shell='bash')
            with open(env_bake_file, 'w') as f:
                f.write(script)
        except Exception, err:
            # error("rez-build failed - an environment failed to resolve.\n" + str(err))
            if os.path.exists(dot_file):
                os.remove(dot_file)
            if os.path.exists(env_bake_file):
                os.remove(env_bake_file)
            raise

        # TODO: this shouldn't be a separate step
        # create dot-file
        # rez-config --print-dot --time=$opts.time $self.requires $variant > $dot_file

        # TODO: convert this to a rex.Command list and generate script using a CommandInterpreter
        text = textwrap.dedent("""\
			#!/bin/bash

			# because of how cmake works, you must cd into same dir as script to run it
			if [ "./build-env.sh" != "$0" ] ; then
				echo "you must cd into the same directory as this script to use it." >&2
				exit 1
			fi

			source %(env_bake_file)s
			export REZ_CONTEXT_FILE=%(env_bake_file)s
			env > %(actual_bake)s

			# need to expose rez-config's cmake modules in build env
			[[ CMAKE_MODULE_PATH ]] && export CMAKE_MODULE_PATH=%(rez_path)s/cmake';'$CMAKE_MODULE_PATH || export CMAKE_MODULE_PATH=%(rez_path)s/cmake

			# make sure we can still use rez-config in the build env!
			export PATH=$PATH:%(rez_path)s/bin

			echo
			echo rez-build: in new env:
			rez-context-info

			# set env-vars that CMakeLists.txt files can reference, in this way
			# we can drive the build from the package.yaml file
			export REZ_BUILD_ENV=1
			export REZ_BUILD_PROJECT_VERSION=%(version)s
			export REZ_BUILD_PROJECT_NAME=%(name)s
			""" % dict(env_bake_file=env_bake_file,
              actual_bake=actual_bake,
              rez_path=rez.rez_filesys._g_rez_path,
              version=self.metadata.version,
              name=self.metadata.name))

        if self.requires:
            text += "export REZ_BUILD_REQUIRES_UNVERSIONED='%s'\n" % (' '.join([unversioned(x) for x in self.requires]))

        if variant:
            text += "export REZ_BUILD_VARIANT='%s'\n" % variant_str
            text += "export REZ_BUILD_VARIANT_UNVERSIONED='%s'\n" % (' '.join([unversioned(x) for x in variant]))
            text += "export REZ_BUILD_VARIANT_SUBDIR=/%s/\n" % variant_subdir

        if not retain_cmake_cache:
            text += _format_bash_command(["rm", "-f", "CMakeCache.txt"])

        # cmake invocation
        # cmake_dir_arg = os.path.relpath(self.root_dir, build_dir)
        # FIXME: the source is exported to a new location, not self.root_dir
        text += _format_bash_command(["cmake", "-d", self.root_dir] + cmake_args)

        if do_build:
            # TODO: determine build tool from --build-system? For now just assume make

            if make_clean:
                text += _format_bash_command(["make", "clean"])

            text += _format_bash_command(["make"] + make_args)

            with open(src_file, 'w') as f:
                f.write(text + '\n')
            safe_chmod(src_file, 0777)

            # run the build
            # TODO: add the 'cd' into the script itself
            p = subprocess.Popen([os.path.join('.', os.path.basename(src_file))],
                                 cwd=build_dir)
            p.communicate()
            if p.returncode != 0:
                # error("rez-build failed - there was a problem building. returned code %s" % (p.returncode,))
                sys.exit(1)

        else:
            text += 'export REZ_ENV_PROMPT=">$REZ_ENV_PROMPT"\n'
            text += "export REZ_ENV_PROMPT='BUILD>'\n"
            text += "/bin/bash --rcfile %s/bin/rez-env-bashrc\n" % rez.rez_filesys._g_rez_path

            with open(src_file, 'w') as f:
                f.write(text + '\n')
            safe_chmod(src_file, 0777)

            if variant:
                print "Generated %s, invoke to run cmake for this project's variant:(%s)" % (src_file, variant_str)
            else:
                print "Generated %s, invoke to run cmake for this project." % src_file

    def get_source(self):
        return builds.get_patched_source(self.metadata)

    # phases ---------
    def init(self, central_release=False):
        '''
        Fill out variables based on metadata
        '''
        self.release_install = central_release

        if self.release_install:
            self.base_build_dir = os.path.join(self.root_dir, 'build', 'rez-release')
            install_var = REZ_RELEASE_PATH_ENV_VAR
        else:
            self.base_build_dir = os.path.join(self.root_dir, 'build')
            install_var = "REZ_LOCAL_PACKAGES_PATH"

        self.base_install_dir = os.getenv(install_var)
        if not self.base_install_dir:
            raise RezReleaseError("$" + install_var + " is not set.")

        self.metadata = self.get_metadata()

        self.family_install_dir = os.path.join(self.base_install_dir, self.metadata.name)
        self.version_install_dir = os.path.join(self.family_install_dir, self.metadata.version)

        self.variants = self.metadata.get_variants()
        if not self.variants:
            self.variants = [None]

        self.requires = self.metadata.get_requires(include_build_reqs=True) or []

        self.changelog = self.get_changelog()

    def pre_build(self):
        '''
        Fill out variables and check for problems
        '''
        self.package_uuid_file = os.path.join(self.family_install_dir, "package.uuid")

        self.package_uuid_exists = self.check_uuid(self.package_uuid_file)

        # create base dir to do clean builds from
        if os.path.exists(self.base_build_dir):
            if os.path.islink(self.base_build_dir):
                os.remove(self.base_build_dir)
            elif os.path.isdir(self.base_build_dir):
                print "deleting pre-existing build directory: %s" % self.base_build_dir
                shutil.rmtree(self.base_build_dir)
            else:
                os.remove(self.base_build_dir)

        os.makedirs(self.base_build_dir)

        if (self.commit_message is None):
            # get preferred editor for commit message
            self.editor = os.getenv(EDITOR_ENV_VAR)
            if not self.editor:
                raise RezReleaseError("rez-release: $" + EDITOR_ENV_VAR + " is not set.")
            self.commit_message = ''

        # check we're in a state to release (no modified/out-of-date files etc)
        self.validate_repostate()

        self.validate_version()

        self._get_commit_message()

    def build(self):
        '''
        Perform build of all variants
        '''
        # svn-export each variant out to a clean directory, and build it locally. If any
        # builds fail then this release is aborted

        print
        print("---------------------------------------------------------")
        print("rez-release: building...")
        print("---------------------------------------------------------")

        # TODO: merge get_source and copy_source.
        # copy_source is meant to be "clean" (i.e. no accidental edits)
        # and get_source should already meet this requirement. It would be good to add a
        # non-dev(e.g.release) mode to get_source which causes it to use 'hg archive',
        # 'git archive', 'svn export' from the cached repos.
        # lastly, it would be nice to not have to get/copy source for each variant,
        # as this can take awhile, and the source should be guaranteed to be the
        # same for all variants anyway.
        self.get_source()

# 		for varnum, variant in enumerate(self.variants):
# 			self.build_variant(varnum)

    def build_variant(self, variant_num):
        '''
        Build a single variant
        '''
        variant = self.variants[variant_num]
        if variant:
            build_dir = os.path.join(self.base_build_dir, str(variant_num))
            varname = "project variant #" + str(variant_num)
        else:
            build_dir = self.base_build_dir
            varname = "project"

# 		print
# 		print("rez-release: creating clean copy of " + varname + " to " + build_dir + "...")
# 		if os.path.exists(build_dir):
# 			shutil.rmtree(build_dir)
# 		self.copy_source(build_dir)

        self._build_variant(variant_num)

    def install(self):
        '''
        Perform installation of all variants
        '''
        # now install the variants
        print
        print("---------------------------------------------------------")
        print("rez-release: installing...")
        print("---------------------------------------------------------")

        # create the package.uuid file, if it doesn't exist
        if not self.package_uuid_exists:
            if not os.path.exists(self.family_install_dir):
                os.makedirs(self.family_install_dir)

            f = open(self.package_uuid_file, 'w')
            f.write(self.metadata.uuid)
            f.close()

        # install the variants
        for varnum, variant in enumerate(self.variants):
            self.install_variant(varnum)

    def install_variant(self, variant_num):
        '''
        Install a single variant
        '''
        variant = self.variants[variant_num]
        if variant:
            varname = "project variant #" + str(variant_num)
            install_path = os.path.join(self.version_install_dir, *variant)
        else:
            variant_num = ''
            varname = 'project'
            install_path = os.path.join(self.version_install_dir)
        subdir = os.path.join(self.base_build_dir, str(variant_num))

        print
        print("rez-release: installing " + varname + " from " + subdir + " to " + install_path + "...")

        # FIXME: ideally we would not re-run _build_variant() here just to install,
        # because it does a lot of redundant work just for installing: e.g. environment resolution,
        # cmake discovery. We do this extra work in order to change 'make' to 'make install'.
        # so, either figure out a way to do that more efficiently, or simply run
        # 'make install' the first time.
        self._build_variant(variant_num, make_args=['install'], make_clean=False)

        # run rez-build, and:
        # * manually specify the svn-url to write into self.metadata;
        # * manually specify the changelog file to use
        # these steps are needed because the code we're building has been svn-exported, thus
        # we don't have any svn context.

        self.check_installed_variant(install_path)

    def post_install(self):
        '''
        Final stage after installation
        '''
        self.write_time_metafile()

        self.send_email()

        print
        print("---------------------------------------------------------")
        print("rez-release: tagging...")
        print("---------------------------------------------------------")
        print

        self.create_release_tag()

        print
        print("rez-release: your package was released successfully.")
        print

register_release_mode(RezReleaseMode)

##############################################################################
# Subversion
##############################################################################

class SvnValueCallback(object):
    """
    simple functor class
    """
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return True, self.value

# TODO: remove these functions once everything is consolidated onto the SvnRezReleaseMode class

def svn_get_client():
    import pysvn
    # check we're in an svn working copy
    client = pysvn.Client()
    client.set_interactive(True)
    client.set_auth_cache(False)
    client.set_store_passwords(False)
    client.callback_get_login = getSvnLogin
    return client

def svn_url_exists(client, url):
    """
    return True if the svn url exists
    """
    import pysvn
    try:
        svnlist = client.info2(url, recurse=False)
        return len(svnlist) > 0
    except pysvn.ClientError:
        return False

def get_last_changed_revision(client, url):
    """
    util func, get last revision of url
    """
    import pysvn
    try:
        svn_entries = client.info2(url,
                                   pysvn.Revision(pysvn.opt_revision_kind.head),
                                   recurse=False)
        if len(svn_entries) == 0:
            raise RezReleaseError("svn.info2() returned no results on url '" + url + "'")
        return svn_entries[0][1].last_changed_rev
    except pysvn.ClientError, ce:
        raise RezReleaseError("svn.info2() raised ClientError: %s" % ce)

def getSvnLogin(realm, username, may_save):
    """
    provide svn with permissions. @TODO this will have to be updated to take
    into account automated releases etc.
    """
    import getpass

    print "svn requires a password for the user '" + username + "':"
    pwd = ''
    while(pwd.strip() == ''):
        pwd = getpass.getpass("--> ")

    return True, username, pwd, False

class SvnRezReleaseMode(RezReleaseMode):
    name = 'svn'

    def __init__(self, path):
        super(SvnRezReleaseMode, self).__init__(path)

        try:
            import pysvn
        except ImportError:
            raise RezReleaseUnsupportedMode("pysvn python module must be installed to properly release a project under subversion.")

        self.svnc = svn_get_client()
        svn_entry = self.svnc.info(self.root_dir)
        if not svn_entry:
            raise RezReleaseUnsupportedMode("'" + self.root_dir + "' is not an svn working copy")
        self.this_url = str(svn_entry["url"])

        # variables filled out in pre_build()
        self.tag_url = None

    def get_tag_url(self, version=None):
        # find the base path, ie where 'trunk', 'branches', 'tags' should be
        pos_tr = self.this_url.find("/trunk")
        pos_br = self.this_url.find("/branches")
        pos = max(pos_tr, pos_br)
        if (pos == -1):
            raise RezReleaseError(self.root_dir + "is not in a branch or trunk")
        base_url = self.this_url[:pos]
        tag_url = base_url + "/tags"

        if version:
            tag_url += '/' + str(version)
        return tag_url

    def svn_url_exists(self, url):
        return svn_url_exists(self.svnc, url)

    def get_last_tagged_revision(self):
        '''
        Return the revision number as int, and tag url of the last tag
        '''
        tag_url = self.get_tag_url()
        latest_tag_url = tag_url + '/' + str(self.last_tagged_version)
        latest_rev = get_last_changed_revision(self.svnc, latest_tag_url)

        return latest_rev.number, latest_tag_url

    # Overrides ------
    @classmethod
    def is_valid_root(cls, path):
        """
        Return True if this release mode works with the given root path
        """
        return os.path.isdir(os.path.join(path, '.svn'))

    def get_url(self):
        '''
        Return a string for the remote url that best identifies the source of
        this VCS repository.
        '''
        return self.this_url

    def get_tag_meta_str(self):
        '''
        Return a tag identifier string for this VCS.
        Could be a url, revision, hash, etc.
        Cannot contain spaces, dashes, or newlines.
        '''
        return self.tag_url

    def get_tags(self):
        tag_url = self.get_tag_url()

        if not self.svn_url_exists(tag_url):
            raise RezReleaseError("Tag url does not exist: " + tag_url)

        # read all the tags (if any) and find the most recent
        tags = self.svnc.ls(tag_url)
        if len(tags) == 0:
            raise RezReleaseError("No existing tags")

        tags = []
        for tag_entry in tags:
            tag = tag_entry["name"].split('/')[-1]
            if tag[0] == 'v':
                # old launcher-style vXX_XX_XX
                nums = tag[1:].split('_')
                tag = str(int(nums[0])) + '.' + str(int(nums[1])) + '.' + str(int(nums[2]))
            tags.append(tag)
        return tags

    def get_last_tagged_version(self):
        """
        returns a rez Version
        """
        if '/branches/' in self.this_url:
            # create a Version instance from the branch we are on this makes sure it's
            # a Well Formed Version, and also puts the base version in 'latest_ver'
            latest_ver = versions.Version(self.this_url.split('/')[-1])
        else:
            latest_ver = versions.Version("0")

        found_tag = False
        for tag in self.get_tags():
            try:
                ver = versions.Version(tag)
            except Exception:
                continue

            if ver > latest_ver:
                latest_ver = ver
                found_tag = True

        if not found_tag:
            return
        return latest_ver

    def validate_version(self):
        self.tag_url = self.get_tag_url(self.version)
        # check that this tag does not already exist
        if self.svn_url_exists(self.tag_url):
            raise RezReleaseError("cannot release: the tag '"
                                  + self.tag_url + "' already exists in svn." +
                                  " You may need to up your version, svn-checkin and try again.")

        super(SvnRezReleaseMode, self).validate_version()

    def validate_repostate(self):
        status_list = self.svnc.status(self.root_dir, get_all=False, update=True)
        status_list_known = []
        for status in status_list:
            if status.entry:
                status_list_known.append(status)
        if len(status_list_known) > 0:
            raise RezReleaseError("'" + self.root_dir + "' is not in a state to "
                                  "release - you may need to "
                                  "svn-checkin and/or svn-update: " +
                                  str(status_list_known))

        # do an update
        print("rez-release: svn-updating...")
        self.svnc.update(self.root_dir)

    def create_release_tag(self):
        # at this point all variants have built and installed successfully. Copy to the new tag
        print("rez-release: creating project tag in: " + self.tag_url + "...")
        self.svnc.callback_get_log_message = SvnValueCallback(self.commit_message)

        self.svnc.copy2([(self.this_url,)], self.tag_url, make_parents=True)

    def get_metadata(self):
        result = super(SvnRezReleaseMode, self).get_metadata()
        # check that ./package.yaml is under svn control
        if not self.svn_url_exists(self.this_url + "/package.yaml"):
            raise RezReleaseError(self.root_dir + "/package.yaml is not under source control")
        return result

    def copy_source(self, build_dir):
        # svn-export it. pysvn is giving me some false assertion crap on 'is_canonical(self.root_dir)' here, hence shell
        pret = subprocess.Popen(["svn", "export", self.this_url, build_dir])
        pret.communicate()
        if (pret.returncode != 0):
            raise RezReleaseError("rez-release: svn export failed")

    def get_changelog(self):
        # Get the changelog.
        import pysvn
        try:
            result = self.get_last_tagged_revision()
        except (ImportError, RezReleaseError):
            log = "Changelog since first revision, tag:(NONE)\n"
            # svn log -r HEAD:1 --stop-on-copy
            end = pysvn.Revision(pysvn.opt_revision_kind.number, 1)
        else:
            if result is None:
                log = "Changelog since first branch revision:(NONE)\n"
                # svn log -r HEAD:1 --stop-on-copy
                end = pysvn.Revision(pysvn.opt_revision_kind.number, 1)
            else:
                rev, tagurl = result
                log = "Changelog since rev: %d tag: %s\n" % (rev, tagurl)
                # svn log -r HEAD:$rev
                end = pysvn.Revision(pysvn.opt_revision_kind.number, rev)
        start = pysvn.Revision(pysvn.opt_revision_kind.head)
        log += self.svnc.log(start, end, strict_node_history=True)
        return log

# 		pret = subprocess.Popen("rez-svn-changelog",
# 							    stdout=subprocess.PIPE,
# 							    stderr=subprocess.PIPE)
# 		changelog, changelog_err = pret.communicate()
# 		return changelog

register_release_mode(SvnRezReleaseMode)


##############################################################################
# Mercurial
##############################################################################

def hg(*args):
    """
    call the `hg` executable with the list of arguments provided.
    Return a list of output lines if the call is successful, else raise RezReleaseError
    """
    cmd = ['hg'] + list(args)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode:
        # TODO: create a new error type and add the error string to an attribute
        raise RezReleaseError("failed to call: hg " + ' '.join(args) + '\n' + err)
    out = out.rstrip('\n')
    if not out:
        return []
    return out.split('\n')

class HgRezReleaseMode(RezReleaseMode):
    name = 'hg'

    def __init__(self, path):
        super(HgRezReleaseMode, self).__init__(path)

        hgdir = os.path.join(self.root_dir, '.hg')
        if not os.path.isdir(hgdir):
            raise RezReleaseUnsupportedMode("'" + self.root_dir + "' is not an mercurial working copy")

        try:
            assert hg('root')[0] == self.root_dir
        except AssertionError:
            raise RezReleaseUnsupportedMode("'" + self.root_dir + "' is not the root of a mercurial working copy")
        except Exception as err:
            raise RezReleaseUnsupportedMode("failed to call hg binary: " + str(err))

        self.patch_path = os.path.join(hgdir, 'patches')
        if not os.path.isdir(self.patch_path):
            self.patch_path = None

    @classmethod
    def is_valid_root(cls, path):
        """
        Return True if this release mode works with the given root path
        """
        return os.path.isdir(os.path.join(path, '.hg'))

    def get_url(self):
        '''
        Return a string for the remote url that best identifies the source of
        this VCS repository.
        '''
        try:
            return hg('paths', 'default')
        except RezReleaseError:
            # if the 'default' path does not exist, we don't have any valid identifier
            return

    def create_release_tag(self):
        '''
        On release, it is customary for a VCS to generate a tag
        '''
        if self.patch_path:
            # patch queue
            hg('tag', '-f', str(self.this_version),
               '--message', self.commit_message, '--mq')
            # use a bookmark on the main repo since we can't change it
            hg('bookmark', '-f', str(self.this_version))
        else:
            hg('tag', '-f', str(self.this_version))

    def get_tags(self):
        tags = [line.split()[0] for line in hg('tags')]
        bookmarks = [line.split()[-2] for line in hg('bookmarks')]
        return tags + bookmarks

    def validate_repostate(self):
        def _check(modified, path):
            if modified:
                modified = [line.split()[-1] for line in modified]
                raise RezReleaseError("'" + path + "' is not in a state to release" +
                                      " - please commit outstanding changes: " +
                                      ', '.join(modified))

        _check(hg('status', '-m', '-a'), self.root_dir)

        if self.patch_path:
            _check(hg('status', '-m', '-a', '--mq'), self.patch_path)

    def get_tag_meta_str(self):
        if self.patch_path:
            qparent = hg('log', '-r', 'qparent', '--template', '{node}')[0]
            mq_parent = hg('parent', '--mq', '--template', '{node}')[0]
            return qparent + '#' + mq_parent
        else:
            return hg('parent', '--template' '{node}')[0]

    def copy_source(self, build_dir):
        hg('archive', build_dir)

    def get_changelog(self):
        start_rev = str(self.last_tagged_version) if self.last_tagged_version else '0'
        end_rev = 'qparent' if self.patch_path else 'tip'
        log = hg('log', '-r',
                        '%s..%s and not merge()' % (start_rev, end_rev),
                        '--template="{desc}\n\n"')
        return ''.join(log)


register_release_mode(HgRezReleaseMode)

#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.
