#!/usr/bin/env python
"""
Generates an image repo from a set of image specs in the form::

    #property1=value1
    #property2=value2
    command1
    command2

Where the command lines are valid virt-builder commands (see
http://libguestfs.org/virt-builder.1.html)

The specs should be in $PWD/image-specs and the repository will be generated at
$PWD/image-repo

Lago templates repo
====================
To generate a lago template repo, it requires for each spec to have the
properties, see :class:`LagoSpec`

Virt-builder templates repo
============================
For the virt-builder repo format, you'll need for each spec to have the
properties, see :class:`VirtBuilderSpec`

"""
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections import namedtuple
from datetime import datetime


LOGGER = logging.getLogger(__name__)


UncompressedTplInfo = namedtuple(
    'UncompressedTplInfo',
    'size checksum_sha1 checksum_sha512'
)


class Spec(object):
    prop_regex = re.compile(
        '^#(?P<prop_key>\w+)\s*=\s*(?P<prop_value>.*)\s*$'
    )
    required_props = set((
        'name',
    ))

    def __init__(self, props, commands_file):
        self.props = props
        self.commands_file = commands_file

    @classmethod
    def from_spec_file(cls, spec_file):
        props = {}
        with open(spec_file) as spec_fd:
            for line in spec_fd.readlines():
                match = cls.prop_regex.match(line)
                if match:
                    prop = match.groupdict()
                    props[prop['prop_key']] = prop['prop_value']
        new_spec = cls(props=props, commands_file=spec_file)
        new_spec.verify()
        return new_spec

    def verify(self):
        "Implement in your own subclass if needed"
        missing_props = set(self.required_props) - set(self.props.keys())
        if missing_props:
            raise Exception(
                'Malformed spec file %s, missing props %s'
                % (self.commands_file, self.required_props)
            )


class LagoSpec(Spec):
    """
    Lago spec file format, required props:

    * base: virt-builder base image to generate this one on
    * name: Name of the template
    * distro: Distribution of the template os (fc23/el7...)
    """
    required_props = Spec.required_props.union(set((
        'base',
        'name',
        'distro',
    )))


class VirtBuilderSpec(Spec):
    """
    Virt builder spec file format, required props:

    * base: virt-builder base image to generate this one
    * name: Name of the template
    * osinfo: OS information for the image, to show when listing
    * arch: arch for the image, usually x86_64
    * expand: The disk partition to expand when generating images from the
        template, like /dev/sda3
    """
    required_props = Spec.required_props.union(set((
        'base',
        'osinfo',
        'arch',
        'expand',
    )))


class AllSpec(Spec):
    """
    Spec file format that has to match all the other specs, required props:
    """
    required_props = LagoSpec.required_props.union(
        VirtBuilderSpec.required_props
    )


def call(command):
    """
    Wrapper around subprocess.call to add logging and rise erron on usuccessful
    command execution

    Args:
        command (list): command to execute, as passed to
            :func:`subprocess.call`

    Returns:
        None

    Raises:
        RuntimeError: if the command failed
    """
    LOGGER.debug('\n\t'.join("'%s'" % arg for arg in command))
    return_code = subprocess.call(command)
    if return_code != 0:
        raise RuntimeError(
            "Failed to execute command %s"
            % '\n\t'.join("'%s'" % arg for arg in command),
        )


def get_hash(file_path, checksum='sha1'):
    """
    Generate a lago-compatible hasd for the given file

    Args:
        file_path (str): Path to the file to generate the hash for
        checksum (str): hash to apply, one of the supported by hashlib, for
            example sha1 or sha512

    Returns:
        str: Lago compatible hash for that file
    """
    sha = getattr(hashlib, checksum)()
    with open(file_path) as file_descriptor:
        while True:
            chunk = file_descriptor.read(65536)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def build_template(
    commands_file,
    dst_template,
    base_image,
    root_pass='123456',
    with_update=False,
):
    """
    Generates a new ready to use template

    Args:
        commands_file (str): Path to the commands file to use
        dst_template (str): Path for the newly generated disk template
        base_image (str): specification of the base image, for example
            'fedora23', see virt-builder --list
        root_pass (str): Password to use for the root user
        with_update (bool): if passed, will update the system os (yum/dnf/apt)

    Returns:
        UncompressedTplInfo: size and checksum of the uncompressed template
    """
    build_disk_image(
        commands_file=commands_file,
        dst_image=dst_template,
        base_image=base_image,
        root_pass=root_pass,
        with_update=with_update,
    )
    uncompressed_size = prepare_disk_template(disk_image=dst_template)
    uncompressed_checksum_sha1 = get_hash(dst_template, checksum='sha1')
    uncompressed_checksum_sha512 = get_hash(dst_template, checksum='sha512')

    uncompressed_info = UncompressedTplInfo(
        size=uncompressed_size,
        checksum_sha1=uncompressed_checksum_sha1,
        checksum_sha512=uncompressed_checksum_sha512,
    )

    LOGGER.info('    Removing temporary image %s', dst_template)
    os.unlink(dst_template)

    return uncompressed_info


def build_disk_image(
    commands_file,
    dst_image,
    base_image,
    root_pass='123456',
    with_update=False
):
    """
    Generates an uncompressed disk image

    Args:
        commands_file (str): Path to the commands file to use
        dst_image (str): Path for the newly generated disk image
        base_image (str): specification of the base image, for example
            'fedora23', see virt-builder --list
        root_pass (str): Password to use for the root user
        with_update (bool): if passed, will update the system os (yum/dnf/apt)

    Returns:
        None
    """
    LOGGER.info('    Building disk image %s', dst_image)
    command = [
        'virt-builder',
        '--commands-from-file=' + commands_file,
        '--output=' + dst_image,
        '--root-password=password:' + root_pass,
        '--format=qcow2'
    ]
    if with_update:
        command.append('--update')
    command.append(base_image)
    call(command)


def prepare_disk_template(disk_image):
    """
    Generates an template from a disk image, stripping any unnecessary data
    and compressing it.

    Note:
        It destroys the original image

    Args:
        base_disk_image (str): path to the image to generate the template from

    Returns:
        int: uncompressed template size
    """
    LOGGER.info('    Cleaning up disk image %s', disk_image)
    image_cleanup_command = [
        'virt-sysprep',
        '--format=qcow2',
        '--add=' + disk_image,
    ]
    call(image_cleanup_command)

    LOGGER.info('    Sparsifying image %s', disk_image)
    image_sparse_command = [
        'virt-sparsify',
        '--format=qcow2',
        '--in-place',
        disk_image,
    ]
    call(image_sparse_command)

    uncompressed_size = os.stat(disk_image).st_size

    LOGGER.info('    Compressing disk image %s', disk_image)
    compress_command = [
        'xz',
        '--compress',
        '--keep',
        '--threads=0',
        '--best',
        '--force',
        '--verbose',
        '--block-size=16777216',  # from virt-builder page they recommend it
        disk_image
    ]
    call(compress_command)
    return uncompressed_size


def image_path_from_spec(dst_dir, spec, compressed=True):
    """
    Generate the resultant image file path

    Args:
        dst_dir (str): Path where the image was generated
        spec (Spec): spec for the image
        compressed (bool): if False will return the path to the uncompressed
            image

    Returns:
        str: path where the image should be generated
    """
    return os.path.join(
        dst_dir,
        os.path.basename(spec.commands_file) + (compressed and '.xz' or '')
    )


def generate_image(spec, dst_dir, with_update=False):
    """
    Generates a disk image

    Args:
        spec (Spec): specification for this image
        dstr_dir (str): path to put the image data in
        with_update (bool): if passed, will update the system os (yum/dnf/apt)

    Returns:
        tuple(str, tuple(int, str)): the path to the generated compressed
            image and the size + checksum it had before being compressed
    """
    dst_image_path = image_path_from_spec(dst_dir, spec, compressed=False)
    uncompressed_info = build_template(
        commands_file=spec.commands_file,
        dst_template=dst_image_path,
        base_image=spec.props['base'],
        with_update=with_update,
    )
    return (
        dst_image_path + '.xz',
        uncompressed_info
    )


def generate_lago_image_metadata(spec, image_path, uncompressed_info):
    """
    Generates all the metadata needed by lago for the given image

    Args:
        spec (LagoSpec): specification for this image
        image_path (str): path for the image
        uncompressed_info (UncompressedTplInfo): info of the uncompressed
            image

    Returns:
        None
    """
    image_dir = os.path.dirname(image_path)
    metadata_path = os.path.join(
        image_dir,
        os.path.basename(spec.commands_file) + '.metadata'
    )
    with open(metadata_path, 'w') as metadata_fd:
        metadata_fd.write(json.dumps(spec.props))

    hash_path = os.path.join(
        image_dir,
        os.path.basename(spec.commands_file) + '.hash'
    )
    with open(hash_path, 'w') as hash_fd:
        hash_fd.write(uncompressed_info.checksum_sha1)


def generate_lago_repo_metadata(repo_dir, repo_name, url):
    """
    Generates the json metadata file for this repo, as needed to be used by the
    lago clients

    Args:
        repo_dir (str): Repo to generate the metadata file for
        name (str): Name of this repo
        url (str): External URL for this repo

    Returns:
        None
    """
    templates = {}
    metadata = {
        'name': repo_name,
        'templates': templates,
    }
    _, _, files = os.walk(repo_dir).next()
    for file_name in files:
        if not file_name.endswith('.xz'):
            continue
        name = file_name.rsplit('.', 1)[0]
        templates[name] = {
            "versions": {
                "latest": {
                    "source": repo_name,
                    "handle": file_name.rsplit('.', 1)[0],
                    "timestamp": os.stat(
                        os.path.join(repo_dir, file_name)
                    ).st_mtime,
                },
            },
        }

    metadata['sources'] = {
        repo_name: {
            "args": {
                "baseurl": url,
            },
            "type": "http"
        }
    }

    with open(os.path.join(repo_dir, 'repo.metadata'), 'w') as fd:
        fd.write(json.dumps(metadata))


def get_virt_builder_image_metadata(spec, image_path, uncompressed_info):
    """
    Gets all the metadata for the given spec and image file

    Args:
        spec (VirtBuilderSpec): Spec for the given image
        image_path (str): Path to the image
        uncompressed_info (UncompressedTplInfo): info of the uncompressed
            images

    Returns:
        str: Metadata for the given image and spec
    """
    LOGGER.debug(
        'Getting virt-builder metadata for spec %s and images %s',
        spec,
        image_path,
    )
    props = dict(spec.props)

    compressed_size = os.stat(image_path).st_size

    metadata_lines = [
        '[lago-%s]' % os.path.basename(image_path).rsplit('.')[0],
        'file=%s' % os.path.basename(image_path),
        'format=qcow2',
        'compressed_size=%s' % compressed_size,
        'size=%s' % uncompressed_info.size,
        'uncompressed_checksum=%s' % uncompressed_info.checksum_sha512,
        'checksum=%s' % get_hash(image_path, checksum='sha512'),
        'revision=%d' % int(datetime.now().strftime('%Y%m%d%H%M%S')),
    ]

    for prop_name, prop_value in props.items():
        metadata_lines.append('%s=%s' % (prop_name, prop_value))

    return '\n'.join(metadata_lines)


def generate_virt_builder_repo_metadata(repo_dir, specs, uncompressed_infos):
    """
    Generates the index metadata file for this repo, as needed to be used
    by the virt-builder client

    Args:
        repo_dir (str): Repo to generate the metadata file for
        specs (list of str): Spec files to generate the metadata for
        uncompressed_infos (dict of str: UncompressedTplInfo): uncompressed
            images info

    Returns:
        None
    """
    specs_metadata = []

    for spec_path in specs:
        spec = VirtBuilderSpec.from_spec_file(spec_path)
        image_path = image_path_from_spec(repo_dir, spec, compressed=True)
        tpl_uncompressed_info = uncompressed_infos[image_path]
        specs_metadata.append(
            get_virt_builder_image_metadata(
                spec,
                image_path,
                tpl_uncompressed_info,
            )
        )

    with open(os.path.join(repo_dir, 'index'), 'w') as fd:
        fd.write('\n\n'.join(specs_metadata) + '\n')


def generate_repo(
    specs,
    repo_dir,
    base_url,
    repo_name,
    repo_format='all',
    with_update=False
):
    """
    Generates the images from the given specs in the repo_dir

    Args:
        specs (list of str): list of spec paths to generate
        repo_dir (str): Path to the dir to generate the repo on
        base_url (str): full http URL for this repo
        repo_name (str): Name for this repo (for the metadata)
        repo_format (one of 'all', 'lago', 'virt-builder'): Format to generate
            the metadata of the repo
        with_update (bool): if passed, will update the system os (yum/dnf/apt)

    Returns:
        None
    """
    LOGGER.info('Creating repo for specs %s', ','.join(specs))

    if not os.path.exists(repo_dir):
        os.makedirs(repo_dir)

    if repo_format == 'lago':
        spec_cls = LagoSpec
    elif repo_format == 'virt-builder':
        spec_cls = VirtBuilderSpec
    else:
        spec_cls = AllSpec

    # needed to generate some metadata
    # right now, the uncompressed size and checksum:
    uncompressed_infos = {}

    for spec in specs:
        LOGGER.info('')
        LOGGER.info('  Creating template for  %s', spec)
        spec = spec_cls.from_spec_file(spec)
        compressed_image_path, uncompressed_info = generate_image(
            spec=spec,
            dst_dir=repo_dir,
            with_update=with_update,
        )
        uncompressed_infos.update(dict([(
            compressed_image_path,
            uncompressed_info,
        )]))

        if repo_format in ['lago', 'all']:
            image_path = image_path_from_spec(
                dst_dir=repo_dir,
                spec=spec,
                compressed=False,
            )
            generate_lago_image_metadata(
                spec=spec,
                image_path=image_path,
                uncompressed_info=uncompressed_info,
            )

    # Generate main metadata
    if repo_format in ['lago', 'all']:
        generate_lago_repo_metadata(
            repo_dir=repo_dir,
            repo_name=repo_name,
            url=base_url,
        )

    if repo_format in ['virt-builder', 'all']:
        generate_virt_builder_repo_metadata(
            repo_dir=repo_dir,
            uncompressed_infos=uncompressed_infos,
            specs=specs,
        )
    LOGGER.info('Done')


def resolve_specs(paths):
    """
    Given a list of paths, return the list of specfiles

    Args:
        paths (list): paths to look for specs, can be directories or files

    Returns:
        list: expanded spec file paths
    """
    specs = []
    for path in paths:
        if os.path.isdir(path):
            _, _, files = os.walk(path).next()
            specs.extend(os.path.join(path, fname) for fname in files)
        else:
            specs.append(path)
    return specs


def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument(
        '-f', '--repo-format',
        choices=['virt-builder', 'lago', 'all'],
        default='all',
        help='Type of image repo to generate, default=%(default)s',
    )
    parser.add_argument(
        '-s', '--specs', action='append',
        help=(
            'Path to the specs directory or specific spec file, can be '
            'passed more than once, if not passed will use $PWD/image-specs'
        )
    )
    parser.add_argument(
        '-o', '--repo-dir',
        default=os.path.join(os.curdir, 'image-repo'),
        help='Path to generate the repo on, default=%(default)s',
    )
    parser.add_argument(
        '--dont-update-images-os', action='store_false',
        dest='update_images_os',
        help='If passed, will update the image os with yum/dnf/apt-get update'
    )
    parser.add_argument(
        '--base-url', required=True,
        help=(
            'Base url for this repo, so it will generate a working '
            'repo.metadata file that can be used by lago'
        )
    )
    parser.add_argument(
        '--repo-name', required=True,
        help='name for the repo, used by lago metadata'
    )
    args = parser.parse_args(args)

    if args.verbose:
        log_level = logging.DEBUG
        log_format = (
            '%(asctime)s::%(levelname)s::'
            '%(name)s.%(funcName)s:%(lineno)d::'
            '%(message)s'
        )
    else:
        log_level = logging.INFO
        log_format = (
            '%(asctime)s::%(levelname)s::'
            '%(message)s'
        )
    logging.basicConfig(level=log_level, format=log_format)

    LOGGER.debug(args)

    specs_paths = resolve_specs(
        args.specs or [os.path.join(os.curdir, 'image-specs')]
    )
    generate_repo(
        specs=specs_paths,
        repo_dir=args.repo_dir,
        with_update=args.update_images_os,
        base_url=args.base_url,
        repo_name=args.repo_name,
        repo_format=args.repo_format,
    )


if __name__ == '__main__':
    main(sys.argv[1:])
