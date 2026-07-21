-- Migration 001: reimbursements table
--
-- A single table serves confirmed records, pending state, and dedup. The
-- UNIQUE(image_hash) constraint enforces duplicate detection natively. Pending
-- submissions are rows with status = 'pending'.
--
-- Apply via the Supabase SQL editor (Dashboard → SQL Editor → New query → paste
-- → Run) or `psql` against the project's connection string.

create table reimbursements (
    submission_id   uuid        primary key default gen_random_uuid(),
    user_id         text        not null,
    file_id         text,
    employee_name   text,
    email           text,
    vendor          text,
    date            date,
    total           numeric(12, 2),
    currency        text        default 'USD',
    category        text,
    tax             numeric(12, 2),
    line_items      jsonb,
    image_hash      text        not null,
    status          text        not null default 'pending'
                                check (status in ('pending', 'confirmed', 'error')),
    created_at      timestamptz not null default now(),
    submitted_at    timestamptz,
    constraint uq_reimbursements_image_hash unique (image_hash)
);

create index idx_reimbursements_user_id    on reimbursements (user_id);
create index idx_reimbursements_created_at on reimbursements (created_at);
create index idx_reimbursements_status     on reimbursements (status);
