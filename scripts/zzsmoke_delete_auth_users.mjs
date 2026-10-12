/**
 * ZZSMOKE auth-identity cleanup via the SUPPORTED Supabase Auth Admin API.
 *
 * Deliberately performs NO direct DDL against auth.users, auth.identities,
 * auth.sessions or any other auth internal table. admin.deleteUser() removes
 * the user and its auth-side children in the correct internal order, and
 * refuses cleanly when the user does not exist (404), which is what makes the
 * whole cleanup re-runnable.
 *
 * ORDERING: run this AFTER deploy/ZZSMOKE_CLEANUP.sql. Four NO ACTION foreign
 * keys (exams.created_by, questions.created_by, exam_attempts.student_id,
 * security_events.student_id -> public.profiles) make admin.deleteUser() fail
 * while any fixture application record still references the profile.
 *
 * SECRET HANDLING: the service-role key is read from the SUPABASE_SERVICE_ROLE_KEY
 * environment variable. It is never written to disk, never logged, never printed,
 * and never passed on a command line (which would leak it into shell history and
 * the process list).
 *
 * Usage (PowerShell):
 *   $env:ZZSMOKE_TEACHER_UUID = '...'
 *   $env:ZZSMOKE_STUDENT_UUID = '...'
 *   $env:SUPABASE_URL           = 'https://<project-ref>.supabase.co'
 *   $env:SUPABASE_SERVICE_ROLE_KEY = '<service-role key>'
 *   node scripts/zzsmoke_delete_auth_users.mjs
 *   $env:SUPABASE_SERVICE_ROLE_KEY = ''      # scrub it from the session
 */
import { createClient } from '@supabase/supabase-js';

const url     = process.env.SUPABASE_URL;
const key     = process.env.SUPABASE_SERVICE_ROLE_KEY;
const teacher = process.env.ZZSMOKE_TEACHER_UUID;
const student = process.env.ZZSMOKE_STUDENT_UUID;

if (!url || !key || !teacher || !student) {
  console.error('ABORT: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ZZSMOKE_TEACHER_UUID and '
              + 'ZZSMOKE_STUDENT_UUID must all be set.');
  process.exit(2);
}
if (teacher === student) {
  console.error('ABORT: teacher and student UUIDs are identical.');
  process.exit(2);
}

// A malformed UUID would be rejected by the API anyway; catch it before any call.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
for (const [label, id] of [['teacher', teacher], ['student', student]]) {
  if (!UUID_RE.test(id)) {
    console.error(`ABORT: ${label} UUID is not a valid UUID.`);
    process.exit(2);
  }
}

const supabase = createClient(url, key, {
  auth: { autoRefreshToken: false, persistSession: false },
});

// Gate 1: both identities must exist AND look like ZZSMOKE fixtures.
// If either is not a fixture we stop before issuing any deletion.
console.log('Gate 1 - verifying both identities are ZZSMOKE fixtures...');
const { data: rows, error: listErr } = await supabase
  .from('profiles')
  .select('id, full_name, role')
  .in('id', [teacher, student]);

if (listErr) {
  console.error(`ABORT: could not read profiles: ${listErr.message}`);
  process.exit(3);
}
// profiles are gone by now; that is expected and fine - the gate then falls
// back to the Auth API itself for the email check below.
if (rows && rows.length > 0) {
  for (const r of rows) {
    if (!String(r.full_name ?? '').toUpperCase().startsWith('ZZSMOKE')) {
      console.error(`ABORT: ${r.id} has full_name "${r.full_name}", not a ZZSMOKE fixture.`);
      process.exit(3);
    }
  }
  console.log(`  profiles still present and all ZZSMOKE (${rows.length} row(s))`);
} else {
  console.log('  profiles already removed by ZZSMOKE_CLEANUP.sql - skipping profile gate');
}

console.log('Gate 2 - verifying the auth identities are ZZSMOKE fixtures...');
for (const [label, id] of [['teacher', teacher], ['student', student]]) {
  const { data, error } = await supabase.auth.admin.getUserById(id);
  if (error) {
    console.error(`ABORT: cannot read ${label} ${id}: ${error.message}`);
    process.exit(3);
  }
  if (!data?.user) {
    console.log(`  ${label} ${id} already absent - nothing to delete`);
    continue;
  }
  const email = String(data.user.email ?? '');
  if (!email.toUpperCase().includes('ZZSMOKE')) {
    console.error(`ABORT: ${label} ${id} has email "${email}", which is not a ZZSMOKE fixture.`);
    process.exit(3);
  }
  console.log(`  ${label} ${id} verified as ${email}`);
}

console.log('Gate 3 - deleting via the Auth Admin API...');
let failures = 0;
for (const [label, id] of [['teacher', teacher], ['student', student]]) {
  // Hard delete (shouldSoftDelete defaults to false). A soft delete would only
  // set deleted_at + obfuscate the email, leaving the auth.users row present
  // and therefore failing the byte-exact baseline comparison afterwards.
  const { data, error } = await supabase.auth.admin.deleteUser(id, false);
  if (error) {
    // 404 / "not found" is the desired idempotent outcome, not a failure.
    if (error.status === 404 || /not found/i.test(error.message)) {
      console.log(`  ${label} ${id}: already deleted`);
      continue;
    }
    console.error(`  FAIL ${label} ${id}: ${error.message}`);
    failures += 1;
    continue;
  }
  console.log(`  ${label} ${id}: deleted (user object returned: ${Boolean(data?.user)})`);
}

if (failures) {
  console.error(`\n${failures} deletion(s) FAILED. Re-run after fixing the cause; the operation is idempotent.`);
  process.exit(4);
}

console.log('\nZZSMOKE auth cleanup complete.');
console.log('Now verify:');
console.log('  python scripts/zzsmoke_baseline_fingerprint.py compare');
process.exit(0);