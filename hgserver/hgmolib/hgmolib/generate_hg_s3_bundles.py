# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import

import argparse
import datetime
import errno
import json
import os
import shutil
import socket
import subprocess
import time

import boto3
import botocore.exceptions
import concurrent.futures as futures
from google.cloud import storage

# Use a separate hg for bundle generation for zstd support until we roll
# out Mercurial 4.1 everywhere.
HG = '/var/hg/venv_bundles/bin/hg'

# The types of bundles to generate.
#
# Define in order bundles should be listed in manifest.
CREATES = [
    ('gzip-v2', ['bundle', '-a', '-t', 'gzip-v2'], {}),
    # ``zstd`` uses default compression settings and is reasonably fast.
    # ``zstd-max`` uses the highest available compression settings and is
    # absurdly slow. But it produces significantly smaller bundles. Level 20
    # (and not higher) is used because it is the largest level supported
    # by the zstd library in 32-bit processes.
    ('zstd', ['bundle', '-a', '-t', 'zstd-v2'], {}),
    ('zstd-max', ['--config', 'experimental.bundlecomplevel=20',
                  'bundle', '-a', '-t', 'zstd-v2'], {}),
    ('packed1', ['debugcreatestreamclonebundle'], {}),
]

CLONEBUNDLES_ORDER = [
    ('zstd-max', 'BUNDLESPEC=zstd-v2'),
    ('zstd', 'BUNDLESPEC=zstd-v2'),
    ('gzip-v2', 'BUNDLESPEC=gzip-v2'),
    ('packed1', 'BUNDLESPEC=none-packed1;requirements%3Dgeneraldelta%2Crevlogv1'),
]

# Defines S3 hostname and bucket where uploads should go.
S3_HOSTS = (
    # We list Oregon (us-west-2) before N. California (us-west-1) because it is
    # cheaper.
    ('s3-us-west-2.amazonaws.com', 'moz-hg-bundles-us-west-2', 'us-west-2'),
    ('s3-us-west-1.amazonaws.com', 'moz-hg-bundles-us-west-1', 'us-west-1'),
    ('s3-us-east-2.amazonaws.com', 'moz-hg-bundles-us-east-2', 'us-east-2'),
    ('s3-external-1.amazonaws.com', 'moz-hg-bundles-us-east-1', 'us-east-1'),
    ('s3-eu-central-1.amazonaws.com', 'moz-hg-bundles-eu-central-1', 'eu-central-1'),
)

# Defines GCP bucket name and region where uploads should go.
# GCP buckets all use the same prefix, unlike AWS
GCP_HOSTS = (
    ('moz-hg-bundles-gcp-us-central1', 'us-central1'),
)

GCS_ENDPOINT = 'https://storage.googleapis.com'

CDN = 'https://hg.cdn.mozilla.net'

BUNDLE_ROOT = '/repo/hg/bundles'

CONCURRENT_THREADS = 4

# Testing backdoor so results are deterministic.
if 'SINGLE_THREADED' in os.environ:
    CONCURRENT_THREADS = 1

HTML_INDEX = '''
<html>
  <head>
    <title>Mercurial Bundles</title>
    <style>
      .numeric {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
    </style>
  </head>
  <body>
    <h1>Mercurial Bundles</h1>
    <p>
       This server contains Mercurial bundle files that can be used to seed
       repository clones. If your Mercurial client is configured properly,
       it should fetch one of these bundles automatically.
    </p>
    <p>
      The table below lists all available repositories and their bundles.
      Only the most recent bundle is shown. Previous bundles are expired 7 days
      after they are superseded.
    </p>
    <p>
      A <a href="bundles.json">JSON document</a> exposes a machine-readable
      representation of this data.
    </p>
    <p>
      <strong>
        Mercurial 4.1 or newer is required to unbundle zstd.
        Please use gzip or stream for older versions.
      </strong>
    </p>
    <p>
       For more, see
       <a href="https://mozilla-version-control-tools.readthedocs.io/en/latest/hgmo/bundleclone.html">the official docs</a>.
    </p>
    <table border="1">
      <tr>
        <th>Repository</th>
        <th>zstd</th>
        <th>zstd (max)</th>
        <th>gzip (v2)</th>
        <th>stream</th>
      </tr>
      %s
    </table>
    <p>This page generated at %s.</p>
  </body>
</html>
'''.strip()

HTML_ENTRY = '''
<tr>
  <td>{repo}</td>
  <td class="numeric">{zstd_entry}</td>
  <td class="numeric">{zstd_max_entry}</td>
  <td class="numeric">{gzip_v2_entry}</td>
  <td class="numeric">{packed1_entry}</td>
</tr>
'''.strip()


def upload_to_s3(region_name, bucket_name, local_path, remote_path):
    """Upload a file to S3."""
    session = boto3.Session(region_name=region_name)

    attempt = 0
    while attempt < 3:
        attempt += 1
        try:
            c = session.resource('s3')

            b = c.Bucket(bucket_name)
            key = b.Object(remote_path)

            # Surprisingly there is no .exists() method. Internet sleuthing
            # revealed this convoluted dance to determine if an object exists.
            try:
                key.load()
                exists = True
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] != '404':
                    raise

                exists = False

            # There is a lifecycle policy on the buckets that expires objects
            # after a few days. If we did nothing here and a repository
            # didn't change for a few days, the bundle objects may become
            # expired and made unavailable.
            #
            # Copying the object resets the modification time to current and
            # prevents unwanted early expiration.
            if exists:
                print('copying %s:%s to reset expiration time' % (bucket_name,
                                                                  remote_path))
                key.copy_from(
                    CopySource={'Bucket': bucket_name, 'Key': remote_path},
                    # This magic is needed to prevent S3 from complaining that
                    # metadata wasn't updated as part of the copy.
                    MetadataDirective='REPLACE')
                print('copying %s:%s completed' % (bucket_name, remote_path))
            else:
                print('uploading %s:%s from %s' % (bucket_name, remote_path,
                                                   local_path))
                key.upload_file(local_path)
                print('uploading %s:%s completed' % (bucket_name, remote_path))

            return
        except socket.error as e:
            print('%s:%s failed: %s' % (bucket_name, remote_path, e))
            time.sleep(15)
    raise Exception('S3 upload of %s:%s not successful after %s attempts, '
                    'giving up' % (bucket_name, remote_path, attempt))


def upload_to_gcpstorage(region_name, bucket_name, local_path, remote_path):
    """Uploads a file to the bucket.

    taken from https://cloud.google.com/python/
    """
    for _attempt in range(3):
        try:
            storage_client = storage.Client()
            bucket = storage_client.get_bucket(bucket_name)
            blob = bucket.blob(remote_path)

            if blob.exists():
                print('resetting expiration time for %s:%s' % (bucket_name, remote_path))

                # Set a temporary hold on an object and then remove the hold, to reset the
                # retention period of the object. See below for details:
                # https://cloud.google.com/storage/docs/bucket-lock#object-holds
                blob.event_based_hold = True
                blob.patch()

                blob.event_based_hold = False
                blob.patch()

                print('expiration time reset for %s:%s' % (bucket_name, remote_path))
            else:
                print('uploading %s:%s from %s' % (bucket_name, remote_path,
                                                   local_path))
                blob.upload_from_filename(local_path)
                print('uploading %s:%s completed' % (bucket_name, remote_path))

            return

        except socket.error as e:
            print('%s:%s failed: %s' % (bucket_name, remote_path, e))
            time.sleep(15)
    else:
        raise Exception('GCP cloud storage upload of %s:%s not successful after'
                        '3 attempts, giving up' % (bucket_name, remote_path))


def bundle_paths(root, repo, tag, typ):
    basename = '%s.%s.hg' % (tag, typ)
    final_path = os.path.join(root, basename)
    remote_path = '%s/%s' % (repo, basename)

    return final_path, remote_path


def generate_bundle(repo, temp_path, final_path, extra_args):
    """Generate a single bundle from arguments.

    Generates using the command specified by ``extra_args`` into ``temp_path``
    before moving the fully created bundle to ``final_path``.
    """
    args = [HG,
            '--config', 'extensions.vcsreplicator=!',
            '-R', repo] + extra_args + [temp_path]
    subprocess.check_call(args)
    os.rename(temp_path, final_path)


def generate_bundles(repo, upload=True, copyfrom=None, zstd_max=False):
    """Generate bundle files for a repository at a path.

    ``zstd_max`` denotes whether to generate zstd bundles with maximum
    compression.
    """
    assert not os.path.isabs(repo)

    # Copy manifest files from the source repository listed. Don't return
    # anything because we don't need to list bundles since this repo isn't
    # canonical.
    if copyfrom:
        # We assume all paths are pinned from a common root.
        assert not os.path.isabs(copyfrom)
        source_repo = os.path.join('/repo/hg/mozilla', copyfrom)
        dest_repo = os.path.join('/repo/hg/mozilla', repo)
        source = os.path.join(source_repo, '.hg', 'clonebundles.manifest')
        dest = os.path.join(dest_repo, '.hg', 'clonebundles.manifest')
        backup = os.path.join(dest_repo, '.hg', 'clonebundles.manifest.last')

        # Create a backup of the last manifest so it can be restored easily.
        if os.path.exists(dest):
            print('copying %s -> %s' % (dest, backup))
            shutil.copy2(dest, backup)

        print('copying %s -> %s' % (source, dest))

        # copy2 copies metadata.
        shutil.copy2(source, dest)

        # Replicate manifest to mirrors.
        subprocess.check_call([HG, 'replicatesync'], cwd=dest_repo)

        return {}

    repo_full = os.path.join('/repo/hg/mozilla', repo)

    hg_stat = os.stat(os.path.join(repo_full, '.hg'))
    uid = hg_stat.st_uid
    gid = hg_stat.st_gid

    # Bundle files are named after the tip revision in the repository at
    # the time the bundle was created. This is the easiest way to name
    # bundle files.
    tip = subprocess.check_output(
        [HG, '-R', repo_full, 'log', '-r', 'tip', '-T', '{node}']
    ).decode('latin-1')
    print('tip is %s' % tip)

    with open(os.path.join(repo_full, '.hg', 'requires'), 'rb') as fh:
        generaldelta = b'generaldelta\n' in fh.readlines()

    if not generaldelta:
        raise Exception('non-generaldelta repo not supported: %s' % repo_full)

    bundle_path = os.path.join(BUNDLE_ROOT, repo)

    # Create directory to hold bundle files.
    try:
        os.makedirs(bundle_path, 0o755)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    # We keep the last bundle files around so we can reuse them if necessary.
    # Prune irrelevant files.
    for p in os.listdir(bundle_path):
        if p.startswith('.') or p.startswith(tip):
            continue

        full = os.path.join(bundle_path, p)
        print('removing old bundle file: %s' % full)
        os.unlink(full)

    # Bundle generation is pretty straightforward. We simply invoke
    # `hg bundle` for each type of bundle we're producing. We use ``-a``
    # to bundle all revisions currently in the repository.
    #
    # There is a race condition between discovering the tip revision and
    # bundling: it's possible for extra revisions beyond observed tip to
    # sneak into the bundles. This is acceptable. Bundles are best effort
    # to offload clone load from the server. They don't have to be exactly
    # identical nor as advertised.
    #
    # We write to temporary files then move them into place after generation.
    # This is because an aborted bundle process may result in a partial file,
    # which may confuse our don't-write-if-file-exists logic.

    bundles = []
    fs = []
    with futures.ThreadPoolExecutor(CONCURRENT_THREADS) as e:
        for t, args, opts in CREATES:
            # Only generate 1 of zstd or zstd-max since they are redundant.
            if t == 'zstd' and zstd_max:
                continue

            if t == 'zstd-max' and not zstd_max:
                continue

            final_path, remote_path = bundle_paths(bundle_path, repo, tip, t)
            temp_path = '%s.tmp' % final_path

            # Record that this bundle is relevant.
            bundles.append((t, final_path, remote_path))

            if os.path.exists(final_path):
                print('bundle already exists, skipping: %s' % final_path)
                continue

            fs.append(e.submit(generate_bundle, repo_full, temp_path,
                               final_path, args))

    for f in fs:
        # Will re-raise exceptions.
        f.result()

    # Object path is keyed off the repository name so we can easily see what
    # is taking up space on the server.
    #
    # We upload directly to each EC2 region. This is worth explaining.
    #
    # S3 supports replication. However, replication occurs asynchronously
    # with the upload. This means there is a window between when upload
    # completes and when the bundle is available in the other region. We
    # don't want to advertise the bundle until it is distributed, as this
    # would result in a 404 and client failure. We could poll and wait for
    # replication to complete. However, there are similar issues with
    # using COPY...
    #
    # There is a COPY API on S3 that allows you to perform a remote copy
    # between regions. This seems like a perfect API, as it saves the
    # client from having to upload the same data to Amazon multiple times.
    # However, we've seen COPY operations take longer to complete than a
    # raw upload. See bug 1167732. Since bundles are being generated in a
    # datacenter that has plentiful bandwidth to S3 and because we
    # generally like operations to complete faster, we choose to simply
    # upload the bundle to multiple regions instead of employ COPY.
    if upload:
        fs = []
        with futures.ThreadPoolExecutor(CONCURRENT_THREADS) as e:
            for host, bucket, name in S3_HOSTS:
                for t, bundle_path, remote_path in bundles:
                    print('uploading to %s/%s/%s' % (host, bucket, remote_path))
                    fs.append(e.submit(upload_to_s3, name, bucket,
                                       bundle_path, remote_path))

            for bucket, region in GCP_HOSTS:
                for t, bundle_path, remote_path in bundles:
                    print('uploading to %s/%s/%s'% (GCS_ENDPOINT, bucket, remote_path))
                    fs.append(e.submit(upload_to_gcpstorage, region, bucket,
                                       bundle_path, remote_path))

        # Future.result() will raise if a future raised. This will
        # abort script execution, which is fine since failure should
        # be rare given how reliable S3 is.
        for f in fs:
            f.result()

    # Now assemble a manifest listing each bundle.
    paths = {}
    for t, final_path, remote_path in bundles:
        paths[t] = (remote_path, os.path.getsize(final_path))

    bundle_types = set(t[0] for t in bundles)

    clonebundles_manifest = []
    for t, params in CLONEBUNDLES_ORDER:
        if t not in bundle_types:
            continue

        final_path, remote_path = bundle_paths(bundle_path, repo, tip, t)
        clonebundles_manifest.append('%s/%s %s REQUIRESNI=true cdn=true' % (
            CDN, remote_path, params))

        # Prefer S3 buckets over GCP buckets for the time being,
        # so add them first
        for host, bucket, name in S3_HOSTS:
            entry = 'https://%s/%s/%s %s ec2region=%s' % (
                host, bucket, remote_path, params, name)
            clonebundles_manifest.append(entry)

        for bucket, name in GCP_HOSTS:
            entry = '%s/%s/%s %s gceregion=%s' % (
                GCS_ENDPOINT, bucket, remote_path, params, name
            )
            clonebundles_manifest.append(entry)

    backup_path = os.path.join(repo_full, '.hg', 'clonebundles.manifest.old')
    clonebundles_path = os.path.join(repo_full, '.hg', 'clonebundles.manifest')

    if os.path.exists(clonebundles_path):
        print('Copying %s -> %s' % (clonebundles_path, backup_path))
        shutil.copy2(clonebundles_path, backup_path)

    with open(clonebundles_path, 'w') as fh:
        fh.write('\n'.join(clonebundles_manifest))

    # Ensure manifest is owned by same user who owns repo and has sane
    # permissions.
    # TODO we can't do this yet since the "hg" user isn't a member of the
    # scm_* groups.
    # os.chown(clonebundles_path, uid, gid)
    os.chmod(clonebundles_path, 0o664)

    # Replicate manifest to mirrors.
    subprocess.check_call([HG, 'replicatesync'], cwd=repo_full)

    return paths


def generate_index(repos):
    """Upload an index HTML page describing available bundles."""
    entries = []

    for repo in sorted(repos):
        p = repos[repo]

        # Should only be for bundles with copyfrom.
        if 'gzip-v2' not in p:
            print('ignoring repo %s in index because no gzip bundle' % repo)
            continue

        opts = {'repo': repo}

        for k in ('gzip-v2', 'packed1', 'zstd', 'zstd-max'):
            key = '%s_entry' % k.replace('-', '_')
            if k in p:
                opts[key] = '<a href="{path}">{size:,}</a>'.format(
                    path=p[k][0],
                    size=p[k][1],
                )
                opts['basename'] = os.path.basename(p[k][0])
            else:
                opts[key] = '-'

        entries.append(HTML_ENTRY.format(**opts))

    html = HTML_INDEX % ('\n'.join(entries),
                         datetime.datetime.utcnow().isoformat())

    # We rely on the mtime of this file for monitoring to ensure
    # bundle generation is working.
    with open(os.path.join(BUNDLE_ROOT, 'index.html'), 'w') as fh:
        fh.write(html)

    return html


def upload_index(html):
    for host, bucket, region in S3_HOSTS:
        client = boto3.client('s3', region_name=region)
        client.put_object(
            Bucket=bucket,
            Key='index.html',
            Body=html,
            ContentType='text/html',
            # Without this, the CDN caches objects for an indeterminate amount
            # of time. We want this page to be fairly current, so establish a
            # less aggressive caching policy.
            CacheControl='max-age=60',
        )


def generate_json_manifest(repos):
    d = {}
    for repo, bundles in repos.items():
        if not bundles:
            continue

        d[repo] = {}
        for t, (path, size) in bundles.items():
            d[repo][t] = {
                'path': path,
                'size': size,
            }

    data = json.dumps(d, sort_keys=True, indent=4)
    with open(os.path.join(BUNDLE_ROOT, 'bundles.json'), 'w') as fh:
        fh.write(data)

    return data


def upload_json_manifest(data):
    for host, bucket, region in S3_HOSTS:
        c = boto3.client('s3', region_name=region)
        c.put_object(
            Bucket=bucket,
            Key='bundles.json',
            Body=data,
            ContentType='application/json',
            CacheControl='max-age=60',
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', help='file to read repository list from')
    parser.add_argument('--no-upload', action='store_true',
                        help='do not upload to servers (useful for testing)')

    repos = []

    args, remaining = parser.parse_known_args()
    if args.f:
        with open(args.f, 'r') as fh:
            items = [l.rstrip() for l in fh]
    else:
        items = remaining

    for item in items:
        attrs = {}
        fields = item.split()
        # fields[0] is the repo path.
        for field in fields[1:]:
            vals = field.split('=', 1)
            if len(vals) == 2:
                attrs[vals[0]] = vals[1]
            else:
                attrs[vals[0]] = True

        repos.append((fields[0], attrs))

    upload = not args.no_upload

    paths = {}

    try:
        for repo, opts in repos:
            paths[repo] = generate_bundles(repo, upload=upload, **opts)
    except (botocore.exceptions.NoCredentialsError, OSError) as e:
        print('%s: %s' % (e.__class__.__name__, e))
        return 1

    html_index = generate_index(paths)
    json_manifest = generate_json_manifest(paths)

    if upload:
        upload_index(html_index)
        upload_json_manifest(json_manifest)

    # Touch a file after successful execution. We monitor this file's age
    # and alert when the bundle generation process is busted.
    with open(os.path.join(BUNDLE_ROOT, 'lastrun'), 'w') as fh:
        fh.write('%sZ\n' % datetime.datetime.utcnow().isoformat())

