# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pathlib
import re
import tarfile

import jsonschema
import yaml

from pythonbuild.logging import log

EXTENSION_MODULE_SCHEMA = {
    "type": "object",
    "properties": {
        "config-c-only": {"type": "boolean"},
        "defines": {"type": "array", "items": {"type": "string"}},
        "defines-conditional": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "define": {"type": "string"},
                    "targets": {"type": "array", "items": {"type": "string"}},
                    "minimum-python-version": {"type": "string"},
                    "maximum-python-version": {"type": "string"},
                },
                "additionalProperties": False,
                "required": ["define"],
            },
        },
        "disabled-targets": {"type": "array", "items": {"type": "string"}},
        "frameworks": {"type": "array", "items": {"type": "string"}},
        "includes": {"type": "array", "items": {"type": "string"}},
        "includes-conditional": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "targets": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
        "includes-deps": {"type": "array", "items": {"type": "string"}},
        "links": {"type": "array", "items": {"type": "string"}},
        "links-conditional": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "targets": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
        "linker-args": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "args": {"type": "array", "items": {"type": "string"}},
                    "targets": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
        "minimum-python-version": {"type": "string"},
        "maximum-python-version": {"type": "string"},
        "required-targets": {"type": "array", "items": {"type": "string"}},
        "setup-enabled": {"type": "boolean"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "sources-conditional": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "targets": {"type": "array", "items": {"type": "string"}},
                    "minimum-python-version": {"type": "string"},
                    "maximum-python-version": {"type": "string"},
                },
                "additionalProperties": False,
                "required": ["source"],
            },
        },
    },
    "additionalProperties": False,
}

EXTENSION_MODULES_SCHEMA = {
    "type": "object",
    "patternProperties": {
        "^[a-z_]+$": EXTENSION_MODULE_SCHEMA,
    },
}


# Packages that define tests.
STDLIB_TEST_PACKAGES = {
    "bsddb.test",
    "ctypes.test",
    "distutils.tests",
    "email.test",
    "idlelib.idle_test",
    "json.tests",
    "lib-tk.test",
    "lib2to3.tests",
    "sqlite3.test",
    "test",
    "tkinter.test",
    "unittest.test",
}


def parse_setup_line(line: bytes, python_version: str):
    """Parse a line in a ``Setup.*`` file."""
    if b"#" in line:
        line = line[: line.index(b"#")].rstrip()

    if not line:
        return

    words = line.split()

    extension = words[0].decode("ascii")

    objs = set()
    links = set()
    frameworks = set()

    for i, word in enumerate(words):
        # Arguments looking like C source files are converted to object files.
        if word.endswith(b".c"):
            # Object files are named according to the basename: parent
            # directories they may happen to reside in are stripped out.
            source_path = pathlib.Path(word.decode("ascii"))

            # Python 3.11 changed the path of the object file.
            if meets_python_minimum_version(python_version, "3.11") and b"/" in word:
                obj_path = (
                    pathlib.Path("Modules")
                    / source_path.parent
                    / source_path.with_suffix(".o").name
                )
            else:
                obj_path = pathlib.Path("Modules") / source_path.with_suffix(".o").name

            objs.add(obj_path)

        # Arguments looking like link libraries are converted to library
        # dependencies.
        elif word.startswith(b"-l"):
            links.add(word[2:].decode("ascii"))

        elif word.startswith(b"-hidden-l"):
            links.add(word[len("-hidden-l") :].decode("ascii"))

        elif word == b"-framework":
            frameworks.add(words[i + 1].decode("ascii"))

    return {
        "extension": extension,
        "line": line,
        "posix_obj_paths": objs,
        "links": links,
        "frameworks": frameworks,
        "variant": "default",
    }


def link_for_target(lib: str, target_triple: str) -> str:
    # TODO use -Wl,-hidden-lbz2?
    # TODO use -Wl,--exclude-libs,libfoo.a?
    if "-apple-" in target_triple:
        return f"-Xlinker -hidden-l{lib}"
    else:
        return f"-l{lib}"


def meets_python_minimum_version(got: str, wanted: str) -> bool:
    parts = got.split(".")
    got_major, got_minor = int(parts[0]), int(parts[1])

    parts = wanted.split(".")
    wanted_major, wanted_minor = int(parts[0]), int(parts[1])

    return (got_major, got_minor) >= (wanted_major, wanted_minor)


def meets_python_maximum_version(got: str, wanted: str) -> bool:
    parts = got.split(".")
    got_major, got_minor = int(parts[0]), int(parts[1])

    parts = wanted.split(".")
    wanted_major, wanted_minor = int(parts[0]), int(parts[1])

    return (got_major, got_minor) <= (wanted_major, wanted_minor)


def derive_setup_local(
    cpython_source_archive,
    python_version,
    target_triple,
    extension_modules,
):
    """Derive the content of the Modules/Setup.local file."""

    # The first part of this function validates that our extension modules YAML
    # based metadata is in sync with the various files declaring extension
    # modules in the Python distribution.

    disabled = set()
    ignored = set()
    setup_enabled_wanted = set()
    config_c_only_wanted = set()

    # Collect metadata about our extension modules as they relate to this
    # Python target.
    for name, info in sorted(extension_modules.items()):
        python_min_match = meets_python_minimum_version(
            python_version, info.get("minimum-python-version", "1.0")
        )
        python_max_match = meets_python_maximum_version(
            python_version, info.get("maximum-python-version", "100.0")
        )

        if not (python_min_match and python_max_match):
            log(f"ignoring extension module {name} because Python version incompatible")
            ignored.add(name)
            continue

        if targets := info.get("disabled-targets"):
            if any(re.match(p, target_triple) for p in targets):
                log(
                    "disabling extension module %s because disabled for this target triple"
                    % name
                )
                disabled.add(name)

        if info.get("setup-enabled", False):
            setup_enabled_wanted.add(name)

        if info.get("config-c-only"):
            config_c_only_wanted.add(name)

    # Parse more files in the distribution for their metadata.

    with tarfile.open(str(cpython_source_archive)) as tf:
        ifh = tf.extractfile("Python-%s/Modules/Setup" % python_version)
        setup_lines = ifh.readlines()

        ifh = tf.extractfile("Python-%s/Modules/config.c.in" % python_version)
        config_c_in = ifh.read()

    dist_modules = set()
    setup_enabled_actual = set()

    RE_VARIABLE = re.compile(rb"^[a-zA-Z_]+\s*=")
    RE_EXTENSION_MODULE = re.compile(rb"^([a-z_]+)\s.*[a-zA-Z/_-]+\.c\b")

    for line in setup_lines:
        line = line.rstrip()

        if not line:
            continue

        # Looks like a variable assignment.
        if RE_VARIABLE.match(line):
            continue

        # Look for extension syntax before and after comment.
        for i, part in enumerate(line.split(b"#")):
            if m := RE_EXTENSION_MODULE.match(part):
                dist_modules.add(m.group(1).decode("ascii"))

                if i == 0:
                    setup_enabled_actual.add(m.group(1).decode("ascii"))

                break

    config_c_extensions = parse_config_c(config_c_in.decode("utf-8"))

    for extension in sorted(config_c_extensions):
        dist_modules.add(extension)

    # With ours and theirs extension module metadata collections, compare and
    # make sure our metadata is comprehensive. This isn't strictly necessary.
    # But it makes it drastically easier to catch bugs due to our metadata being
    # out of sync with the distribution. This has historically caused several
    # subtle and hard-to-diagnose bugs, which is why we do it.

    missing = dist_modules - set(extension_modules.keys())

    if missing:
        raise Exception(
            "missing extension modules from YAML: %s" % ", ".join(sorted(missing))
        )

    missing = setup_enabled_actual - setup_enabled_wanted
    if missing:
        raise Exception(
            "Setup enabled extensions missing YAML setup-enabled annotation: %s"
            % ", ".join(sorted(missing))
        )

    extra = setup_enabled_wanted - setup_enabled_actual
    if extra:
        raise Exception(
            "YAML setup-enabled extensions not present in Setup: %s"
            % ", ".join(sorted(extra))
        )

    if missing := set(config_c_extensions) - config_c_only_wanted:
        raise Exception(
            "config.c.in extensions missing YAML config-c-only annotation: %s"
            % ", ".join(sorted(missing))
        )

    if extra := config_c_only_wanted - set(config_c_extensions):
        raise Exception(
            "YAML config-c-only extensions not present in config.c.in: %s"
            % ", ".join(sorted(extra))
        )

    # And with verification out of way, now we generate a Setup.local file
    # from our metadata. The verification above ensured that our metadata
    # agrees fully with the distribution's knowledge of extensions. So we can
    # treat our metadata as canonical.

    RE_DEFINE = re.compile(rb"-D[^=]+=[^\s]+")

    # Translate our YAML metadata into Setup lines.

    # All extensions are statically linked.
    dest_lines = [b"*static*"]

    # makesetup parses lines with = as extra config options. There appears
    # to be no easy way to define e.g. -Dfoo=bar in Setup.local. We hack
    # around this by producing a Makefile supplement that overrides the build
    # rules for certain targets to include these missing values.
    extra_cflags = {}

    for name in sorted(extension_modules.keys()):
        if name in disabled or name in ignored:
            continue

        info = extension_modules[name]

        if "sources" not in info:
            continue

        log(f"deriving Setup line for {name}")

        line = name

        for source in info.get("sources", []):
            line += " %s" % source

        for entry in info.get("sources-conditional", []):
            if targets := entry.get("targets", []):
                target_match = any(re.match(p, target_triple) for p in targets)
            else:
                target_match = True

            python_min_match = meets_python_minimum_version(
                python_version, entry.get("minimum-python-version", "1.0")
            )
            python_max_match = meets_python_maximum_version(
                python_version, entry.get("maximum-python-version", "100.0")
            )

            if target_match and (python_min_match and python_max_match):
                line += f" {entry['source']}"

        for define in info.get("defines", []):
            line += f" -D{define}"

        for entry in info.get("defines-conditional", []):
            if targets := entry.get("targets", []):
                target_match = any(re.match(p, target_triple) for p in targets)
            else:
                target_match = True

            python_min_match = meets_python_minimum_version(
                python_version, entry.get("minimum-python-version", "1.0")
            )
            python_max_match = meets_python_maximum_version(
                python_version, entry.get("minimum-python-version", "100.0")
            )

            if target_match and (python_min_match and python_max_match):
                line += f" -D{entry['define']}"

        for path in info.get("includes", []):
            line += f" -I{path}"

        for entry in info.get("includes-conditional", []):
            if any(re.match(p, target_triple) for p in entry["targets"]):
                line += f" -I{entry['path']}"

        for path in info.get("includes-deps", []):
            # Includes are added to global search path.
            if "-apple-" in target_triple:
                continue

            line += f" -I/tools/deps/{path}"

        for lib in info.get("links", []):
            line += " %s" % link_for_target(lib, target_triple)

        for entry in info.get("links-conditional", []):
            if any(re.match(p, target_triple) for p in entry["targets"]):
                line += " %s" % link_for_target(entry["name"], target_triple)

        if "-apple-" in target_triple:
            for framework in info.get("frameworks", []):
                line += f" -framework {framework}"

        for entry in info.get("linker-args", []):
            if any(re.match(p, target_triple) for p in entry["targets"]):
                for arg in entry["args"]:
                    line += f" -Xlinker {arg}"

        line = line.encode("ascii")

        # This extra parse is a holder from older code and could likely be
        # factored away.
        parsed = parse_setup_line(line, python_version=python_version)

        if not parsed:
            raise Exception("we should always parse a setup line we generated")

        # makesetup parses lines with = as extra config options. There appears
        # to be no easy way to define e.g. -Dfoo=bar in Setup.local. We hack
        # around this by detecting the syntax we'd like to support and move the
        # variable defines to a Makefile supplement that overrides variables for
        # specific targets.
        for m in RE_DEFINE.finditer(parsed["line"]):
            for obj_path in sorted(parsed["posix_obj_paths"]):
                extra_cflags.setdefault(bytes(obj_path), []).append(m.group(0))

        line = RE_DEFINE.sub(b"", line)

        if b"=" in line:
            raise Exception(
                "= appears in EXTRA_MODULES line; will confuse "
                "makesetup: %s" % line.decode("utf-8")
            )
        dest_lines.append(line)

    dest_lines.append(b"\n*disabled*\n")
    dest_lines.extend(sorted(x.encode("ascii") for x in disabled))

    dest_lines.append(b"")

    make_lines = []

    for target in sorted(extra_cflags):
        make_lines.append(
            b"%s: PY_STDMODULE_CFLAGS += %s" % (target, b" ".join(extra_cflags[target]))
        )

    return {
        "config_c_extensions": config_c_extensions,
        "setup_dist": b"\n".join(setup_lines),
        "setup_local": b"\n".join(dest_lines),
        "make_data": b"\n".join(make_lines),
    }


RE_INITTAB_ENTRY = re.compile('\{"([^"]+)", ([^\}]+)\},')


def parse_config_c(s: str):
    """Parse the contents of a config.c file.

    The file defines external symbols for module init functions and the
    mapping of module name to module initializer function.
    """

    # Some config.c files have #ifdef. We don't care about those because
    # in all cases the condition is true.

    extensions = {}

    seen_inittab = False

    for line in s.splitlines():
        if line.startswith("struct _inittab"):
            seen_inittab = True

        if not seen_inittab:
            continue

        if "/* Sentinel */" in line:
            break

        m = RE_INITTAB_ENTRY.search(line)

        if m:
            extensions[m.group(1)] = m.group(2)

    return extensions


def extension_modules_config(yaml_path: pathlib.Path):
    """Loads the extension-modules.yml file."""
    with yaml_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=yaml.SafeLoader)

    jsonschema.validate(data, EXTENSION_MODULES_SCHEMA)

    return data
