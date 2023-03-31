# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import os
import xml.etree.ElementTree as ET
import re
import requests
import yaml
import synthtool as s
import synthtool.gcp as gcp
from synthtool import cache, shell
from synthtool.gcp import common, partials, pregenerated, samples, snippets
from synthtool.log import logger
from pathlib import Path
from typing import Any, Optional, Dict, Iterable, List

JAR_DOWNLOAD_URL = "https://github.com/google/google-java-format/releases/download/google-java-format-{version}/google-java-format-{version}-all-deps.jar"
DEFAULT_FORMAT_VERSION = "1.7"
GOOD_LICENSE = """/*
 * Copyright 2020 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
"""
PROTOBUF_HEADER = "// Generated by the protocol buffer compiler.  DO NOT EDIT!"
BAD_LICENSE = """/\\*
 \\* Copyright \\d{4} Google LLC
 \\*
 \\* Licensed under the Apache License, Version 2.0 \\(the "License"\\); you may not use this file except
 \\* in compliance with the License. You may obtain a copy of the License at
 \\*
 \\* http://www.apache.org/licenses/LICENSE-2.0
 \\*
 \\* Unless required by applicable law or agreed to in writing, software distributed under the License
 \\* is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
 \\* or implied. See the License for the specific language governing permissions and limitations under
 \\* the License.
 \\*/
"""
DEFAULT_MIN_SUPPORTED_JAVA_VERSION = 8


def format_code(
    path: str, version: str = DEFAULT_FORMAT_VERSION, times: int = 2
) -> None:
    """
    Runs the google-java-format jar against all .java files found within the
    provided path.
    """
    jar_name = f"google-java-format-{version}.jar"
    jar = cache.get_cache_dir() / jar_name
    if not jar.exists():
        _download_formatter(version, jar)

    # Find all .java files in path and run the formatter on them
    files = list(glob.iglob(os.path.join(path, "**/*.java"), recursive=True))

    # Run the formatter as a jar file
    logger.info("Running java formatter on {} files".format(len(files)))
    for _ in range(times):
        shell.run(["java", "-jar", str(jar), "--replace"] + files)


def _download_formatter(version: str, dest: Path) -> None:
    logger.info("Downloading java formatter")
    url = JAR_DOWNLOAD_URL.format(version=version)
    response = requests.get(url)
    response.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(response.content)


HEADER_REGEX = re.compile("\\* Copyright \\d{4} Google LLC")


def _file_has_header(path: Path) -> bool:
    """Return true if the file already contains a license header."""
    with open(path, "rt") as fp:
        for line in fp:
            if HEADER_REGEX.search(line):
                return True
    return False


def _filter_no_header(paths: Iterable[Path]) -> Iterable[Path]:
    """Return a subset of files that do not already have a header."""
    for path in paths:
        anchor = Path(path.anchor)
        remainder = str(path.relative_to(path.anchor))
        for file in anchor.glob(remainder):
            if not _file_has_header(file):
                yield file


def fix_proto_headers(proto_root: Path) -> None:
    """Helper to ensure that generated proto classes have appropriate license headers.

    If the file does not already contain a license header, inject one at the top of the file.
    Some resource name classes may contain malformed license headers. In those cases, replace
    those with our standard license header.
    """
    s.replace(
        _filter_no_header([proto_root / "src/**/*.java"]),
        PROTOBUF_HEADER,
        f"{GOOD_LICENSE}{PROTOBUF_HEADER}",
    )
    # https://github.com/googleapis/gapic-generator/issues/3074
    s.replace(
        [proto_root / "src/**/*Name.java", proto_root / "src/**/*Names.java"],
        BAD_LICENSE,
        GOOD_LICENSE,
    )


def fix_grpc_headers(grpc_root: Path, package_name: str = "unused") -> None:
    """Helper to ensure that generated grpc stub classes have appropriate license headers.

    If the file does not already contain a license header, inject one at the top of the file.
    """
    s.replace(
        _filter_no_header([grpc_root / "src/**/*.java"]),
        "^package (.*);",
        f"{GOOD_LICENSE}package \\1;",
    )


def latest_maven_version(group_id: str, artifact_id: str) -> Optional[str]:
    """Helper function to find the latest released version of a Maven artifact.

    Fetches metadata from Maven Central and parses out the latest released
    version.

    Args:
        group_id (str): The groupId of the Maven artifact
        artifact_id (str): The artifactId of the Maven artifact

    Returns:
        The latest version of the artifact as a string or None
    """
    group_path = "/".join(group_id.split("."))
    url = (
        f"https://repo1.maven.org/maven2/{group_path}/{artifact_id}/maven-metadata.xml"
    )
    response = requests.get(url)
    if response.status_code >= 400:
        return "0.0.0"

    return version_from_maven_metadata(response.text)


def version_from_maven_metadata(metadata: str) -> Optional[str]:
    """Helper function to parse the latest released version from the Maven
    metadata XML file.

    Args:
        metadata (str): The XML contents of the Maven metadata file

    Returns:
        The latest version of the artifact as a string or None
    """
    root = ET.fromstring(metadata)
    latest = root.find("./versioning/latest")
    if latest is not None:
        return latest.text

    return None


def _common_generation(
    service: str,
    version: str,
    library: Path,
    package_pattern: str,
    suffix: str = "",
    destination_name: str = None,
    cloud_api: bool = True,
    diregapic: bool = False,
    preserve_gapic: bool = False,
):
    """Helper function to execution the common generation cleanup actions.

    Fixes headers for protobuf classes and generated gRPC stub services. Copies
    code and samples to their final destinations by convention. Runs the code
    formatter on the generated code.

    Args:
        service (str): Name of the service.
        version (str): Service API version.
        library (Path): Path to the temp directory with the generated library.
        package_pattern (str): Package name template for fixing file headers.
        suffix (str, optional): Suffix that the generated library folder. The
            artman output differs from bazel's output directory. Defaults to "".
        destination_name (str, optional): Override the service name for the
            destination of the output code. Defaults to the service name.
        preserve_gapic (bool, optional): Whether to preserve the gapic directory
            prefix. Default False.
    """

    if destination_name is None:
        destination_name = service

    cloud_prefix = "cloud-" if cloud_api else ""
    package_name = package_pattern.format(service=service, version=version)
    fix_proto_headers(
        library / f"proto-google-{cloud_prefix}{service}-{version}{suffix}"
    )
    fix_grpc_headers(
        library / f"grpc-google-{cloud_prefix}{service}-{version}{suffix}", package_name
    )

    if preserve_gapic:
        s.copy(
            [library / f"gapic-google-{cloud_prefix}{service}-{version}{suffix}/src"],
            f"gapic-google-{cloud_prefix}{destination_name}-{version}/src",
            required=True,
        )
    else:
        s.copy(
            [library / f"gapic-google-{cloud_prefix}{service}-{version}{suffix}/src"],
            f"google-{cloud_prefix}{destination_name}/src",
            required=True,
        )

    s.copy(
        [library / f"grpc-google-{cloud_prefix}{service}-{version}{suffix}/src"],
        f"grpc-google-{cloud_prefix}{destination_name}-{version}/src",
        # For REST-only clients, like java-compute, gRPC artifact does not exist
        required=(not diregapic),
    )
    s.copy(
        [library / f"proto-google-{cloud_prefix}{service}-{version}{suffix}/src"],
        f"proto-google-{cloud_prefix}{destination_name}-{version}/src",
        required=True,
    )

    if preserve_gapic:
        format_code(f"gapic-google-{cloud_prefix}{destination_name}-{version}/src")
    else:
        format_code(f"google-{cloud_prefix}{destination_name}/src")
    format_code(f"grpc-google-{cloud_prefix}{destination_name}-{version}/src")
    format_code(f"proto-google-{cloud_prefix}{destination_name}-{version}/src")


def gapic_library(
    service: str,
    version: str,
    config_pattern: str = "/google/cloud/{service}/artman_{service}_{version}.yaml",
    package_pattern: str = "com.google.cloud.{service}.{version}",
    gapic: gcp.GAPICGenerator = None,
    destination_name: str = None,
    diregapic: bool = False,
    preserve_gapic: bool = False,
    **kwargs,
) -> Path:
    """Generate a Java library using the gapic-generator via artman via Docker.

    Generates code into a temp directory, fixes missing header fields, and
    copies into the expected locations.

    Args:
        service (str): Name of the service.
        version (str): Service API version.
        config_pattern (str, optional): Path template to artman config YAML
            file. Defaults to "/google/cloud/{service}/artman_{service}_{version}.yaml"
        package_pattern (str, optional): Package name template for fixing file
            headers. Defaults to "com.google.cloud.{service}.{version}".
        gapic (GAPICGenerator, optional): Generator instance.
        destination_name (str, optional): Override the service name for the
            destination of the output code. Defaults to the service name.
        preserve_gapic (bool, optional): Whether to preserve the gapic directory
            prefix. Default False.
        **kwargs: Additional options for gapic.java_library()

    Returns:
        The path to the temp directory containing the generated client.
    """
    if gapic is None:
        gapic = gcp.GAPICGenerator()

    library = gapic.java_library(
        service=service,
        version=version,
        config_path=config_pattern.format(service=service, version=version),
        artman_output_name="",
        include_samples=True,
        diregapic=diregapic,
        **kwargs,
    )

    _common_generation(
        service=service,
        version=version,
        library=library,
        package_pattern=package_pattern,
        destination_name=destination_name,
        diregapic=diregapic,
        preserve_gapic=preserve_gapic,
    )

    return library


def bazel_library(
    service: str,
    version: str,
    package_pattern: str = "com.google.cloud.{service}.{version}",
    gapic: gcp.GAPICBazel = None,
    destination_name: str = None,
    cloud_api: bool = True,
    diregapic: bool = False,
    preserve_gapic: bool = False,
    **kwargs,
) -> Path:
    """Generate a Java library using the gapic-generator via bazel.

    Generates code into a temp directory, fixes missing header fields, and
    copies into the expected locations.

    Args:
        service (str): Name of the service.
        version (str): Service API version.
        package_pattern (str, optional): Package name template for fixing file
            headers. Defaults to "com.google.cloud.{service}.{version}".
        gapic (GAPICBazel, optional): Generator instance.
        destination_name (str, optional): Override the service name for the
            destination of the output code. Defaults to the service name.
        preserve_gapic (bool, optional): Whether to preserve the gapic directory
            prefix. Default False.
        **kwargs: Additional options for gapic.java_library()

    Returns:
        The path to the temp directory containing the generated client.
    """
    if gapic is None:
        gapic = gcp.GAPICBazel()

    library = gapic.java_library(
        service=service, version=version, diregapic=diregapic, **kwargs
    )

    _common_generation(
        service=service,
        version=version,
        library=library / f"google-cloud-{service}-{version}-java",
        package_pattern=package_pattern,
        suffix="-java",
        destination_name=destination_name,
        cloud_api=cloud_api,
        diregapic=diregapic,
        preserve_gapic=preserve_gapic,
    )

    return library


def pregenerated_library(
    path: str,
    service: str,
    version: str,
    destination_name: str = None,
    cloud_api: bool = True,
) -> Path:
    """Generate a Java library using the gapic-generator via bazel.

    Generates code into a temp directory, fixes missing header fields, and
    copies into the expected locations.

    Args:
        path (str): Path in googleapis-gen to un-versioned generated code.
        service (str): Name of the service.
        version (str): Service API version.
        destination_name (str, optional): Override the service name for the
            destination of the output code. Defaults to the service name.
        cloud_api (bool, optional): Whether or not this is a cloud API (for naming)

    Returns:
        The path to the temp directory containing the generated client.
    """
    generator = pregenerated.Pregenerated()
    library = generator.generate(path)

    cloud_prefix = "cloud-" if cloud_api else ""
    _common_generation(
        service=service,
        version=version,
        library=library / f"google-{cloud_prefix}{service}-{version}-java",
        package_pattern="unused",
        suffix="-java",
        destination_name=destination_name,
        cloud_api=cloud_api,
    )

    return library


def _merge_release_please(destination_text: str):
    config = yaml.safe_load(destination_text)
    if "handleGHRelease" in config:
        return destination_text

    config["handleGHRelease"] = True

    if "branches" in config:
        for branch in config["branches"]:
            branch["handleGHRelease"] = True
    return yaml.dump(config)


def _merge_common_templates(
    source_text: str, destination_text: str, file_path: Path
) -> str:
    # keep any existing pom.xml
    if file_path.match("pom.xml") or file_path.match("sync-repo-settings.yaml"):
        logger.debug(f"existing pom file found ({file_path}) - keeping the existing")
        return destination_text

    if file_path.match("release-please.yml"):
        return _merge_release_please(destination_text)

    # by default return the newly generated content
    return source_text


def _common_template_metadata() -> Dict[str, Any]:
    metadata = {}  # type: Dict[str, Any]
    repo_metadata = common._load_repo_metadata()
    if repo_metadata:
        metadata["repo"] = repo_metadata
        group_id, artifact_id = repo_metadata["distribution_name"].split(":")

        metadata["latest_version"] = latest_maven_version(
            group_id=group_id, artifact_id=artifact_id
        )

    metadata["latest_bom_version"] = latest_maven_version(
        group_id="com.google.cloud",
        artifact_id="libraries-bom",
    )

    metadata["samples"] = samples.all_samples(["samples/**/src/main/java/**/*.java"])
    metadata["snippets"] = snippets.all_snippets(
        ["samples/**/src/main/java/**/*.java", "samples/**/pom.xml"]
    )
    if repo_metadata and "min_java_version" in repo_metadata:
        metadata["min_java_version"] = repo_metadata["min_java_version"]
    else:
        metadata["min_java_version"] = DEFAULT_MIN_SUPPORTED_JAVA_VERSION

    return metadata


def common_templates(
    excludes: List[str] = [], template_path: Optional[Path] = None, **kwargs
) -> None:
    """Generate common templates for a Java Library

    Fetches information about the repository from the .repo-metadata.json file,
    information about the latest artifact versions and copies the files into
    their expected location.

    Args:
        excludes (List[str], optional): List of template paths to ignore
        **kwargs: Additional options for CommonTemplates.java_library()
    """
    metadata = _common_template_metadata()
    kwargs["metadata"] = metadata

    # Generate flat to tell this repository is a split repo that have migrated
    # to monorepo. The owlbot.py in the monorepo sets monorepo=True.
    monorepo = kwargs.get("monorepo", False)
    kwargs["monorepo"] = monorepo
    split_repo = not monorepo
    repo_metadata = metadata["repo"]
    repo_short = repo_metadata["repo_short"]
    # Special libraries that are not GAPIC_AUTO but in the monorepo
    special_libs_in_monorepo = [
        "java-translate",
        "java-dns",
        "java-notification",
        "java-resourcemanager",
    ]
    kwargs["migrated_split_repo"] = split_repo and (
        repo_metadata["library_type"] == "GAPIC_AUTO"
        or (repo_short and repo_short in special_libs_in_monorepo)
    )
    logger.info(
        "monorepo: {}, split_repo: {}, library_type: {},"
        " repo_short: {}, migrated_split_repo: {}".format(
            monorepo,
            split_repo,
            repo_metadata["library_type"],
            repo_short,
            kwargs["migrated_split_repo"],
        )
    )

    templates = gcp.CommonTemplates(template_path=template_path).java_library(**kwargs)

    # skip README generation on Kokoro (autosynth)
    if os.environ.get("KOKORO_ROOT") is not None:
        # README.md is now synthesized separately. This prevents synthtool from deleting the
        # README as it's no longer generated here.
        excludes.append("README.md")

    s.copy([templates], excludes=excludes, merge=_merge_common_templates)


def custom_templates(files: List[str], **kwargs) -> None:
    """Generate custom template files

    Fetches information about the repository from the .repo-metadata.json file,
    information about the latest artifact versions and copies the files into
    their expected location.

    Args:
        files (List[str], optional): List of template paths to include
        **kwargs: Additional options for CommonTemplates.render()
    """
    kwargs["metadata"] = _common_template_metadata()
    kwargs["metadata"]["partials"] = partials.load_partials()
    for file in files:
        template = gcp.CommonTemplates().render(file, **kwargs)
        s.copy([template])


def remove_method(filename: str, signature: str):
    """Helper to remove an entire method.

    Goes line-by-line to detect the start of the block. Determines
    the end of the block by a closing brace at the same indentation
    level. This requires the file to be correctly formatted.

    Example: consider the following class:

        class Example {
            public void main(String[] args) {
                System.out.println("Hello World");
            }

            public String foo() {
                return "bar";
            }
        }

    To remove the `main` method above, use:

        remove_method('path/to/file', 'public void main(String[] args)')

    Args:
        filename (str): Path to source file
        signature (str): Full signature of the method to remove. Example:
            `public void main(String[] args)`.
    """
    lines = []
    leading_regex = None
    with open(filename, "r") as fp:
        line = fp.readline()
        while line:
            # for each line, try to find the matching
            regex = re.compile("(\\s*)" + re.escape(signature) + ".*")
            match = regex.match(line)
            if match:
                leading_regex = re.compile(match.group(1) + "}")
                line = fp.readline()
                continue

            # not in a ignore block - preserve the line
            if not leading_regex:
                lines.append(line)
                line = fp.readline()
                continue

            # detect the closing tag based on the leading spaces
            match = leading_regex.match(line)
            if match:
                # block is closed, resume capturing content
                leading_regex = None

            line = fp.readline()

    with open(filename, "w") as fp:
        for line in lines:
            # print(line)
            fp.write(line)


def copy_and_rename_method(filename: str, signature: str, before: str, after: str):
    """Helper to make a copy an entire method and rename it.

    Goes line-by-line to detect the start of the block. Determines
    the end of the block by a closing brace at the same indentation
    level. This requires the file to be correctly formatted.
    The method is copied over and renamed in the method signature.
    The calls to both methods are separate and unaffected.

    Example: consider the following class:

        class Example {
            public void main(String[] args) {
                System.out.println("Hello World");
            }

            public String foo() {
                return "bar";
            }
        }

    To copy and rename the `main` method above, use:

    copy_and_rename_method('path/to/file', 'public void main(String[] args)',
        'main', 'foo1')

    Args:
        filename (str): Path to source file
        signature (str): Full signature of the method to remove. Example:
            `public void main(String[] args)`.
        before (str): name of the method to be copied
        after (str): new name of the copied method
    """
    lines = []
    method = []
    leading_regex = None
    with open(filename, "r") as fp:
        line = fp.readline()
        while line:
            # for each line, try to find the matching
            regex = re.compile("(\\s*)" + re.escape(signature) + ".*")
            match = regex.match(line)
            if match:
                leading_regex = re.compile(match.group(1) + "}")
                lines.append(line)
                method.append(line.replace(before, after))
                line = fp.readline()
                continue

            lines.append(line)
            # not in a ignore block - preserve the line
            if leading_regex:
                method.append(line)
            else:
                line = fp.readline()
                continue

            # detect the closing tag based on the leading spaces
            match = leading_regex.match(line)
            if match:
                # block is closed, resume capturing content
                leading_regex = None
                lines.append("\n")
                lines.extend(method)

            line = fp.readline()

    with open(filename, "w") as fp:
        for line in lines:
            # print(line)
            fp.write(line)


def add_javadoc(filename: str, signature: str, javadoc_type: str, content: List[str]):
    """Helper to add a javadoc annoatation to a method.

        Goes line-by-line to detect the start of the block.
        Then finds the existing method comment (if it exists). If the
        comment already exists, it will append the javadoc annotation
        to the javadoc block. Otherwise, it will create a new javadoc
        comment block.

        Example: consider the following class:

            class Example {
                public void main(String[] args) {
                    System.out.println("Hello World");
                }

                public String foo() {
                    return "bar";
                }
            }

        To add a javadoc annotation the `main` method above, use:

        add_javadoc('path/to/file', 'public void main(String[] args)',
            'deprecated', 'Please use foo instead.')

    Args:
        filename (str): Path to source file
        signature (str): Full signature of the method to remove. Example:
            `public void main(String[] args)`.
        javadoc_type (str): The type of javadoc annotation. Example: `deprecated`.
        content (List[str]): The javadoc lines
    """
    lines: List[str] = []
    annotations: List[str] = []
    with open(filename, "r") as fp:
        line = fp.readline()
        while line:
            # for each line, try to find the matching
            regex = re.compile("(\\s*)" + re.escape(signature) + ".*")
            match = regex.match(line)
            if match:
                leading_spaces = len(line) - len(line.lstrip())
                indent = leading_spaces * " "
                last_line = lines.pop()
                while last_line.lstrip() and last_line.lstrip()[0] == "@":
                    annotations.append(last_line)
                    last_line = lines.pop()
                if last_line.strip() == "*/":
                    first = True
                    for content_line in content:
                        if first:
                            lines.append(
                                indent
                                + " * @"
                                + javadoc_type
                                + " "
                                + content_line
                                + "\n"
                            )
                            first = False
                        else:
                            lines.append(indent + " *   " + content_line + "\n")
                    lines.append(last_line)
                else:
                    lines.append(last_line)
                    lines.append(indent + "/**\n")
                    first = True
                    for content_line in content:
                        if first:
                            lines.append(
                                indent
                                + " * @"
                                + javadoc_type
                                + " "
                                + content_line
                                + "\n"
                            )
                            first = False
                        else:
                            lines.append(indent + " *   " + content_line + "\n")
                    lines.append(indent + " */\n")
                lines.extend(annotations[::-1])
            lines.append(line)
            line = fp.readline()

    with open(filename, "w") as fp:
        for line in lines:
            # print(line)
            fp.write(line)


def annotate_method(filename: str, signature: str, annotation: str):
    """Helper to add an annotation to a method.

        Goes line-by-line to detect the start of the block.
        Then adds the annotation above the found method signature.

        Example: consider the following class:

            class Example {
                public void main(String[] args) {
                    System.out.println("Hello World");
                }

                public String foo() {
                    return "bar";
                }
            }

        To add an annotation the `main` method above, use:

        annotate_method('path/to/file', 'public void main(String[] args)',
            '@Generated()')

    Args:
        filename (str): Path to source file
        signature (str): Full signature of the method to remove. Example:
            `public void main(String[] args)`.
        annotation (str): Full annotation. Example: `@Deprecated`
    """
    lines: List[str] = []
    with open(filename, "r") as fp:
        line = fp.readline()
        while line:
            # for each line, try to find the matching
            regex = re.compile("(\\s*)" + re.escape(signature) + ".*")
            match = regex.match(line)
            if match:
                leading_spaces = len(line) - len(line.lstrip())
                indent = leading_spaces * " "
                lines.append(indent + annotation + "\n")
            lines.append(line)
            line = fp.readline()

    with open(filename, "w") as fp:
        for line in lines:
            # print(line)
            fp.write(line)


def deprecate_method(filename: str, signature: str, alternative: str):
    """Helper to deprecate a method.

        Goes line-by-line to detect the start of the block.
        Then adds the deprecation comment before the method signature.
        The @Deprecation annotation is also added.

        Example: consider the following class:

            class Example {
                public void main(String[] args) {
                    System.out.println("Hello World");
                }

                public String foo() {
                    return "bar";
                }
            }

        To deprecate the `main` method above, use:

        deprecate_method('path/to/file', 'public void main(String[] args)',
            DEPRECATION_WARNING.format(new_method="foo"))

    Args:
        filename (str): Path to source file
        signature (str): Full signature of the method to remove. Example:
            `public void main(String[] args)`.
        alternative: DEPRECATION WARNING: multiline javadoc comment with user
            specified leading open/close comment tags
    """
    add_javadoc(filename, signature, "deprecated", alternative.splitlines())
    annotate_method(filename, signature, "@Deprecated")
