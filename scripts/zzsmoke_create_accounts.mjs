/**
 * Create the two disposable ZZSMOKE accounts via the SUPPORTED Supabase Auth
 * Admin API.
 *
 * NO direct writes to auth tables. createUser() lets the database's
 * handle_new_user() trigger build public.profiles, exactly as a real signup
 * does, so the fixture is not special-cased in the database.
 *
 * SECRET HANDLING: the password comes from the ZZSMOKE_PASSWORD environment
 * variable. It is never written to disk, logged, printed, or passed on a
 * command line.
 *
 * Gates before anything is created:
 *   - both emails must be distinct and contain 'zzsmoke'
 *   - neither may already exist (no silent reuse of a stale fixture)
 *
 * Usage (PowerShell):
 *   $env:ZZSMOKE_TEACHER_EMAIL = 'zzsmoke-teacher@<domain>'
 *   $env:ZZSMOKE_STUDENT_EMAIL = 'zzsmoke-student@<domain>'
 *   $env:ZZSMOKE_PASSWORD      = '<a strong throwaway password>'
 *   $env:SUPABASE_URL          = 'https://bhzuyrzlpfkjcqpafzgu.supabase.co'
 *   $env:SUPABASE_SERVICE_ROLE_KEY = '<service-role key>'
 *   node scripts/zzsmoke_create_accounts.mjs
 */
import { createClient } from '@supabase/supabase-js';

const url     = process.env.SUPABASE_URL;
const key     = process.env.SUPABASE_SERVICE_ROLE_KEY;
const teacher = process.env.ZZSMOKE_TEACHER_EMAIL;
const student = process.env.ZZSMOKE_STUDENT_EMAIL;
const pass    = process.env.ZZSMOKE_PASSWORD;

if (!url || !key || !teacher || !student || !pass) {
  console.error('ABORT: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ZZSMOKE_TEACHER_EMAIL, '
              + 'ZZSMOKE_STUDENT_EMAIL and ZZSMOKE_PASSWORD must all be set.');
  process.exit(2);
}
for (const [label, e] of [['teacher', teacher], ['student', student]]) {
  if (!/zzsmoke/i.test(e)) {
    console.error(`ABORT: ${label} email ${e} does not contain 'zzsmoke'.`);
    process.exit(2);
  }
}
if (teacher.toLowerCase() === student.toLowerCase()) {
  console.error('ABORT: the two emails are identical.');
  process.exit(2);
}
if (pass.length < 12) {
  console.error('ABORT: ZZSMOKE_PASSWORD must be at least 12 characters.');
  process.exit(2);
}

const supabase = createClient(url, key, {
  auth: { autoRefreshToken: false, persistSession: false },
});

// Gate: if either address is already registered, stop. A pre-existing
// fixture means an earlier run did not finish; it must be cleaned up rather
// than silently reused.
console.log('Gate - checking neither address is already registered...');
const PAGE = 1000;
let existing = [];
for (let page = 1; page <= 20; page++) {
  const { data, error } = await supabase.auth.admin.listUsers({ page, perPage: PAGE });
  if (error) { console.error(`ABORT: cannot list users: ${error.message}`); process.exit(3); }
  existing = existing.concat(data?.users ?? []);
  if (!data?.users || data.users.length < PAGE) break;
}
const taken = new Set(existing.map((u) => String(u.email ?? '').toLowerCase()));
for (const [label, email] of [['teacher', teacher], ['student', student]]) {
  if (taken.has(email.toLowerCase())) {
    console.error(`ABORT: ${label} address ${email} is ALREADY REGISTERED.`);
    console.error('A previous run did not finish. Run the cleanup procedure before retrying.');
    process.exit(3);
  }
}
console.log(`  neither address is registered (${existing.length} existing account(s))`);

console.log('Creating the two accounts via the Auth Admin API...');
const created = {};
for (const [label, email] of [['teacher', teacher], ['student', student]]) {
  const { data, error } = await supabase.auth.admin.createUser({
    email,
    password: pass,
    email_confirm: true,   // no confirmation round-trip needed for a fixture
  });
  if (error) {
    console.error(`ABORT: could not create ${label} <${email}>: ${error.message}`);
    console.error('If one account was created before the failure, run the partial-failure');
    console.error('cleanup procedure rather than retrying, to avoid duplicate fixtures.');
    process.exit(4);
  }
  created[label] = data.user.id;
  console.log(`  ${label}: ${email} -> ${data.user.id}`);
}

// Confirm handle_new_user() built a profile with role 'student'.
console.log('Verifying the database profiles were created by handle_new_user()...');
const { data: profs, error: pErr } = await supabase
  .from('profiles')
  .select('id, full_name, role')
  .in('id', [created.teacher, created.student]);
if (pErr) { console.error(`ABORT: cannot read profiles: ${pErr.message}`); process.exit(5); }
if (!profs || profs.length !== 2) {
  console.error(`ABORT: expected 2 profiles, found ${profs ? profs.length : 0}.`);
  process.exit(5);
}
for (const p of profs) {
  console.log(`  ${p.id}  ${p.full_name}  role=${p.role}`);
  if (p.role !== 'student') {
    console.error(`ABORT: ${p.id} has role ${p.role}, expected 'student' from handle_new_user.`);
    process.exit(5);
  }
}

console.log('\nBoth accounts created. UUIDs:');
console.log(`  teacher_id = ${created.teacher}`);
console.log(`  student_id = ${created.student}`);
console.log('\nNext:');
console.log('  1. paste these two UUIDs into deploy/ZZSMOKE_BOOTSTRAP.sql and run it');
console.log('  2. record them:  python scripts/zzsmoke_capture_manifest.py verify');