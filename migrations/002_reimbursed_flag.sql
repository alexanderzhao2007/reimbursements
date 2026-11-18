-- Migration 002: reimbursed tracking flag
--
-- Adds a manually-managed flag recording whether a confirmed reimbursement has
-- actually been paid out. Defaults to false (not yet reimbursed) and backfills
-- existing rows as not reimbursed. The trigger auto-stamps `reimbursed_at` when
-- the flag is flipped true (and clears it if flipped back), so marking a row paid
-- in the Supabase table editor stays a single checkbox click.
--
-- The bot never reads or writes these columns — they are operated entirely by
-- hand in Supabase. Filter `reimbursed = false` to see what is still outstanding.
--
-- Apply via Supabase Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Safe to re-run (idempotent guards throughout).

alter table reimbursements
    add column if not exists reimbursed    boolean     not null default false,
    add column if not exists reimbursed_at timestamptz;

create index if not exists idx_reimbursements_reimbursed
    on reimbursements (reimbursed);

create or replace function set_reimbursed_at()
returns trigger as $$
begin
    if new.reimbursed and not old.reimbursed then
        new.reimbursed_at := now();
    elsif not new.reimbursed then
        new.reimbursed_at := null;
    end if;
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_set_reimbursed_at on reimbursements;
create trigger trg_set_reimbursed_at
    before update on reimbursements
    for each row execute function set_reimbursed_at();
