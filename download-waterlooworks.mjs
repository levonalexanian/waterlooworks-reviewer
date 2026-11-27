import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { basename, join } from 'node:path';
import { stdin, stdout } from 'node:process';
import { createInterface } from 'node:readline/promises';
import { pathToFileURL } from 'node:url';
import { parseEnv } from 'node:util';

const root = import.meta.dirname;
const portal = 'https://waterlooworks.uwaterloo.ca';
const loginURL = portal + '/employer/login.htm';
const runtime = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies');

export function jobIds(value) {
  const ids = [...new Set((value || '').split(/[,\s]+/).filter(Boolean))];
  if (!ids.length || ids.some(id => !/^\d{1,12}$/.test(id))) {
    throw new Error('Set WATERLOOWORKS_JOB_IDS in .env to numeric posting IDs separated by commas.');
  }
  return ids;
}

function portalPage(context) {
  const page = context.pages().filter(p => {
    try { return new URL(p.url()).origin === portal; } catch { return false; }
  }).at(-1);
  if (!page) throw new Error('Open WaterlooWorks in the browser launched by this command.');
  return page;
}

async function signIn(context, email, password, ask) {
  const page = await context.newPage();
  await page.goto(loginURL, { waitUntil: 'domcontentloaded' });
  if (!email) {
    await ask('Sign in to WaterlooWorks in Chrome, then press Enter here. ');
  } else {
    if (new URL(page.url()).origin !== portal) {
      await ask('Complete sign-in in Chrome, then press Enter here. ');
      return;
    }
    const emailField = page.getByLabel(/^email:?$/i).or(
      page.locator('input[type=email],input[autocomplete=username],input[name*=email i]'));
    const passwordField = page.locator('input[type=password]');
    const submit = page.getByRole('button', { name: /^(log\s*in|sign\s*in|submit)$/i })
      .or(page.locator('input[type=submit], button[type=submit]')).first();
    try {
      await emailField.filter({ visible: true }).first().fill(email, { timeout: 5000 });
      await passwordField.filter({ visible: true }).fill(password, { timeout: 5000 });
      await submit.click({ timeout: 5000 });
      await page.getByRole('tab', { name: 'Job Postings', exact: true }).waitFor({ timeout: 8000 });
    } catch {
      await ask('Finish sign-in or verification in Chrome, then press Enter here. ');
    }
  }
}

export async function openPosting(context, id, ask) {
  const page = portalPage(context);
  const heading = page.getByRole('heading', { level: 1, name: new RegExp(`^${id}\\s*[-–—]`) });
  if (await heading.isVisible()) return page;
  try {
    const back = page.getByRole('button', { name: /Back to Jobs Overview/i });
    if (await back.isVisible()) await back.click();
    const postings = page.getByRole('tab', { name: 'Job Postings', exact: true });
    if (await postings.isVisible()) {
      await postings.getByRole('link', { name: 'Job Postings', exact: true }).click();
    }

    const idCell = page.getByRole('gridcell', { name: id, exact: true })
      .or(page.getByRole('cell', { name: id, exact: true }));
    const row = page.getByRole('row').filter({ has: idCell });
    await row.getByRole('link', { name: 'View Apps', exact: true }).click({ timeout: 10000 });
    const currentTab = row.getByText('current tab', { exact: true });
    await currentTab.click({ timeout: 5000 });
    await heading.waitFor({ timeout: 10000 });
  } catch {
    await ask(`Open posting ${id} in Chrome, then press Enter. `);
    await heading.waitFor({ timeout: 3000 });
  }
  return page;
}

function applicationsPanel(page) {
  return page.getByRole('tabpanel', { name: /^Applications(?:\s+\d+)?$/i });
}

async function openApplicationOptions(page) {
  const dropdown = page.locator('#applications-options-dropdown');
  // On narrow windows the action bar sits inside an off-screen drawer.
  const onScreen = await dropdown.evaluate(button => {
    const { left, right, width } = button.getBoundingClientRect();
    return width > 0 && left >= 0 && right <= window.innerWidth;
  });
  if (!onScreen) await page.getByRole('button', { name: 'menu', exact: true }).click();
  await dropdown.click();
}

function safeName(filename) {
  return basename(filename.replaceAll('\\', '/')).replace(/[\u0000-\u001f\u007f]/g, '_');
}

async function capture(context, directory, extensions, action) {
  await mkdir(directory, { mode: 0o700 });
  const downloads = [];
  let announce;
  const ready = new Promise(resolve => { announce = resolve; });
  const watching = new Set();
  function receive(download) {
    const name = safeName(download.suggestedFilename());
    if (!extensions.test(name)) return;
    const path = join(directory, `${downloads.length + 1}-${name}`);
    const result = download.saveAs(path).then(() => ({ path }), () => ({ error: true }));
    downloads.push(result);
    announce();
  }
  function watch(page) {
    watching.add(page);
    page.on('download', receive);
  }
  context.pages().forEach(watch);
  context.on('page', watch);
  let timer;
  try {
    await action();
    await Promise.race([ready, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('No matching browser download appeared within two minutes.')), 120000);
    })]);
    // Most bundles arrive as a ZIP. Also keep files triggered together by the portal.
    let count;
    do {
      count = downloads.length;
      await Promise.all(downloads);
      await new Promise(resolve => setTimeout(resolve, 1200));
    } while (count !== downloads.length);
    const results = await Promise.all(downloads);
    if (results.some(result => result.error)) throw new Error('A browser download failed. Check Chrome and rerun this job.');
    return results.map(result => result.path);
  } finally {
    clearTimeout(timer);
    context.off('page', watch);
    watching.forEach(page => page.off('download', receive));
  }
}

export async function downloadRoster(context, page, directory, ask) {
  return capture(context, directory, /\.(csv|xlsx)$/i, async () => {
    try {
      const panel = applicationsPanel(page);
      const total = panel.getByRole('button', { name: /^Total Applications\s+\d+$/i });
      await total.click({ timeout: 8000 });
      await panel.getByRole('link', { name: 'Export', exact: true }).click({ timeout: 8000 });
    } catch {
      await ask('In the Applications overview, choose Total Applications and Export. Press Enter after starting the download. ');
    }
  });
}

export async function downloadBundle(context, page, directory, ask) {
  return capture(context, directory, /\.(zip|pdf)$/i, async () => {
    try {
      const panel = applicationsPanel(page);
      const zipRows = panel.getByRole('row').filter({
        has: page.getByRole('gridcell', { name: 'ZIP', exact: true }),
      });
      await panel.getByRole('link', { name: 'View Application Bundle', exact: true }).click();
      await panel.getByRole('columnheader', { name: 'File Type', exact: true })
        .or(panel.getByText('No Records Found.', { exact: true })).first()
        .waitFor({ timeout: 10000 });
      const previous = new Set(await zipRows.allTextContents());

      await openApplicationOptions(page);
      await page.getByRole('link', { name: 'Download All Applications', exact: true }).click();
      await page.getByRole('button', { name: 'Bundle as Individual PDFs', exact: true }).click();
      await page.getByText('Bundle was successfully created.', { exact: true }).waitFor({ timeout: 90000 });
      await page.getByRole('button', { name: 'X', exact: true }).click();
      await zipRows.first().waitFor({ timeout: 10000 });
      const rows = await zipRows.allTextContents();
      const newRows = rows.flatMap((text, index) => previous.has(text) ? [] : [index]);
      if (newRows.length !== 1) throw new Error('Could not uniquely identify the new ZIP bundle.');
      await zipRows.nth(newRows[0]).getByRole('combobox', { name: 'Options', exact: true })
        .selectOption({ label: 'Download Bundle' });
    } catch {
      await ask('In the posting’s bundle list, choose Download Bundle on the new ZIP. If needed, first create it through Applications Options > Download All Applications > Bundle as Individual PDFs. Press Enter after starting the download. ');
    }
  });
}

function finishBatch(id, directory, roster, bundles) {
  const python = join(runtime, 'python/bin/python3');
  let result = spawnSync(python, [join(root, 'prepare-waterlooworks.py'), id, directory, roster, ...bundles], {
    stdio: 'inherit',
  });
  if (result.error?.code === 'ENOENT') {
    result = spawnSync('python3', [join(root, 'prepare-waterlooworks.py'), id, directory, roster, ...bundles], {
      stdio: 'inherit',
    });
  }
  if (result.status !== 0) throw new Error('The batch did not pass validation. Its original downloads are still in the job folder.');
}

async function main() {
  if (!stdin.isTTY) throw new Error('Run this command in Terminal so you can complete sign-in and any download prompts.');
  const envPath = join(root, '.env');
  const info = await stat(envPath);
  if ((info.mode & 0o077) !== 0) throw new Error('Run chmod 600 .env before adding or using credentials.');
  const config = parseEnv(await readFile(envPath, 'utf8'));
  const ids = jobIds(config.WATERLOOWORKS_JOB_IDS);
  const email = config.WATERLOOWORKS_EMAIL || '';
  const password = config.WATERLOOWORKS_PASSWORD || '';
  if (Boolean(email) !== Boolean(password)) throw new Error('Fill both credential fields, or leave both blank to sign in manually.');
  const require = createRequire(import.meta.url);
  let playwright;
  try { playwright = require('playwright'); }
  catch {
    try { playwright = require(join(runtime, 'node/node_modules/playwright')); }
    catch { throw new Error('Playwright is unavailable. Run npm install --no-save playwright in this folder.'); }
  }

  process.umask(0o077);
  const browser = await playwright.chromium.launch({ channel: 'chrome', headless: false });
  const terminal = createInterface({ input: stdin, output: stdout });
  const ask = message => terminal.question(message);
  try {
    const context = await browser.newContext({ acceptDownloads: true });
    await signIn(context, email, password, ask);
    for (const id of ids) {
      const jobRoot = join(root, 'local', 'waterlooworks', id);
      await mkdir(jobRoot, { recursive: true, mode: 0o700 });
      const batch = await mkdtemp(join(jobRoot, 'batch-'));
      console.log(`Job ${id}: ${batch}`);
      try {
        await openPosting(context, id, ask);
        const roster = await downloadRoster(context, portalPage(context), join(batch, 'roster'), ask);
        if (roster.length !== 1) throw new Error('Expected one applicant roster export. Rerun and export only once.');
        const bundles = await downloadBundle(context, portalPage(context), join(batch, 'bundles'), ask);
        finishBatch(id, batch, roster[0], bundles);
      } catch (error) {
        const message = error instanceof Error ? error.message.split('\n')[0] : 'Download failed.';
        console.error(`Job ${id}: ${message}`);
        process.exitCode = 1;
      }
    }
  } finally {
    terminal.close();
    await browser.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    console.error(error.code === 'ENOENT' ? 'Missing .env. Copy .env.example to .env and run chmod 600 .env.' : error.message.split('\n')[0]);
    process.exitCode = 1;
  });
}
