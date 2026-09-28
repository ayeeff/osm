// Static check for the failure that has bitten the neighbourhood workflow
// repeatedly: a `run:` block expanding a shell variable that nothing in that
// block or its job/step env ever assigns. Under `set -u` that exits 1 with a
// bare "unbound variable" line, and if you are grepping the log you can easily
// miss it.
//
// The earlier ad-hoc version of this check hardcoded the old variable names
// into its allowlist, so it reported "none missing" while CITIES was renamed and
// two call sites still said CITY_COUNT. Nothing may be allowlisted by name here
// except POSIX shell special parameters.
//
// Usage: node scripts/check-workflow-vars.mjs [.github/workflows/*.yml]

import fs from 'node:fs';
import path from 'node:path';

const files = process.argv.slice(2);
if (!files.length) {
  console.error('usage: node scripts/check-workflow-vars.mjs <workflow.yml ...>');
  process.exit(2);
}

// POSIX special parameters and the readonly-ish names GitHub/bash set. Anything
// else must be assigned by the block, the job env, or the step env.
const SHELL_SPECIAL = new Set([
  'BASH', 'BASHOPTS', 'BASH_SOURCE', 'BASHPID', 'EUID', 'FUNCNAME', 'GROUPS',
  'HOSTNAME', 'HOSTTYPE', 'IFS', 'LINENO', 'MACHTYPE', 'OLDPWD', 'OPTARG',
  'OPTERR', 'OSTYPE', 'PIPESTATUS', 'PPID', 'PS1', 'PS2', 'PS4', 'PWD', 'RANDOM',
  'SECONDS', 'SHELL', 'SHELLOPTS', 'UID', '_',
]);

const readStdin = new Set(['$0', '$@', '$*', '$#', '$?', '$-', '$$']);

let problems = 0;

for (const file of files) {
  const text = fs.readFileSync(file, 'utf8');
  const lines = text.split(/\r?\n/);

  // Walk the file tracking the nearest enclosing `jobs:<name>:` and
  // `env:`/`- name:` block so each run block gets the right env keys.
  let jobEnv = new Set();
  let stepEnv = new Set();
  let jobName = null;
  let inJobEnv = false;
  let inStepEnv = false;
  let stepName = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    const jobMatch = line.match(/^ {2}([A-Za-z0-9_-]+):\s*$/);
    if (jobMatch && !trimmed.startsWith('#')) {
      jobName = jobMatch[1];
      jobEnv = new Set();
      inJobEnv = false;
      continue;
    }

    if (/^ {4}env:\s*$/.test(line)) {
      inJobEnv = true;
      inStepEnv = false;
      continue;
    }
    if (/^ {6}env:\s*$/.test(line)) {
      inStepEnv = true;
      inJobEnv = false;
      continue;
    }
    if (/^ {6}- name: /.test(line)) {
      stepName = trimmed.replace('- name: ', '');
      inStepEnv = false;
      continue;
    }

    // env keys: 6 spaces under a job env, 8 under a step env
    if (inJobEnv || inStepEnv) {
      const key = line.match(/^ {6,8}([A-Z_][A-Z0-9_]*):/);
      if (key) {
        (inJobEnv ? jobEnv : stepEnv).add(key[1]);
        continue;
      }
      if (trimmed && !trimmed.startsWith('#')) {
        inJobEnv = false;
        inStepEnv = false;
      }
    }

    // Only examine lines inside a `run: |` block.
    if (!/^ {8,}run:\s*[|>][-+]?\d*\s*$/.test(line)) continue;

    const indent = line.match(/^(\s*)/)[1].length;
    const body = [];
    for (let j = i + 1; j < lines.length; j++) {
      const nxt = lines[j];
      if (nxt.trim() === '') { body.push(''); continue; }
      const nxtIndent = nxt.match(/^(\s*)/)[1].length;
      if (nxtIndent < indent) break;
      body.push(nxt);
      i = j;
    }

    const scope = new Set([...jobEnv, ...stepEnv]);
    const shell = body.find((l) => /^\s*set -[a-z]*u/.test(l)) || '';

    // Assignments, loop targets, and read targets are definitions.
    for (const l of body) {
      for (const m of l.matchAll(/^\s*(?:export\s+|declare\s+-\w+\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=/g)) {
        scope.add(m[1]);
      }
      for (const m of l.matchAll(/\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b/g)) scope.add(m[1]);
      for (const m of l.matchAll(/\bread\s+(?:-[a-zA-Z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)/g)) scope.add(m[1]);
    }

    const used = new Set();
    for (const l of body) {
      // Strip comments so prose does not count as a use, and strip single-quoted
      // spans because a single quote suppresses expansion entirely. That is what
      // makes `--arg s "$SLUG" '...select(.slug == $s)...'` a non-issue: the $s
      // is jq's own variable, not the shell's.
      const code = l
        .replace(/(^|\s)#(?![!{]).*$/, '$1')
        .replace(/'[^']*'/g, "''");
      for (const m of code.matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}|\$([A-Za-z_][A-Za-z0-9_]*)/g)) {
        const name = m[1] || m[2];
        if (name) used.add(name);
      }
    }

    const isGithubProvided = (v) => /^(GITHUB_|CI$|RUNNER_|ACTIONS_)/.test(v);
    const missing = [...used].filter(
      (v) => !scope.has(v) && !SHELL_SPECIAL.has(v) && !isGithubProvided(v)
    );
    if (missing.length) {
      problems += missing.length;
      console.log(`  ${file}  job=${jobName}  step="${stepName ?? 'run'}"  ${shell.trim()}`);
      for (const v of missing.sort()) console.log(`      UNBOUND: $${v}`);
    }
  }
}

console.log(problems ? `\n${problems} unbound variable reference(s)` : '  no unbound variable references');
process.exit(problems ? 1 : 0);
