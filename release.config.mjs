// nb-labs/ci-automation release config (the bump-labeled-MR bot; see the
// `.release` jobs in .gitlab-ci.yml). The fork's release version is the root
// VERSION file — deliberately NOT globals.py's app_version, which stays
// aligned to upstream because plugins gate on it for compatibility checks.
// VERSION is unused by the app itself (the StoreBackend VERSION lookups read
// per-plugin files inside cloned store repos), so the fork claims it.
export default {
  adapter: {
    files: ['VERSION'],
    readVersion: (contents) => contents['VERSION'].trim(),
    stamp: (_contents, version) => ({ VERSION: `${version}\n` }),
  },
  changelogPath: 'CHANGELOG.md',
};
