-- All money-moving logic lives in SECURITY DEFINER functions so that a single
-- tool call is one atomic, race-safe transaction. search_path is locked to ''
-- and every object (including the pgvector <=> operator) is schema-qualified.
-- Execute is revoked from public roles; only service_role (the Edge Function)
-- may call them.

-- Register a new account: create account, grant signup credits, store api key hash.
create or replace function public.scar_register(p_key_hash text, p_key_prefix text, p_label text, p_grant numeric)
returns json language plpgsql security definer set search_path = '' as $$
declare v_account uuid;
begin
  insert into public.accounts(label, balance_credits) values (p_label, 0) returning id into v_account;
  insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id) values (v_account, p_grant, 'signup_grant', 'init');
  update public.accounts set balance_credits = balance_credits + p_grant where id = v_account;
  insert into public.api_keys(account_id, key_hash, key_prefix) values (v_account, p_key_hash, p_key_prefix);
  return json_build_object('account_id', v_account, 'balance_credits', p_grant);
end; $$;

-- Resolve an api key hash to an account id, updating last_used_at.
create or replace function public.scar_auth(p_key_hash text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_account uuid;
begin
  update public.api_keys set last_used_at = now()
    where key_hash = p_key_hash and revoked = false
    returning account_id into v_account;
  return v_account;
end; $$;

-- Query known failures by semantic similarity, then atomically charge the caller.
create or replace function public.scar_check(p_account uuid, p_embedding text, p_threshold double precision, p_base numeric, p_max numeric)
returns json language plpgsql security definer set search_path = '' as $$
declare
  v_emb extensions.vector(384) := p_embedding::extensions.vector(384);
  v_hits json;
  v_top_sim double precision;
  v_top_conf int;
  v_price numeric(14,6);
  v_balance numeric(14,6);
  v_ref text := 'check_' || substr(md5(random()::text),1,8);
begin
  select json_agg(h) into v_hits from (
    select s.id as scar_id, s.failure_mode, s.confirmations, s.context,
           left(s.evidence, 240) as evidence_preview,
           round((extract(epoch from now() - s.submitted_at))::numeric) as age_seconds,
           round((1 - (s.embedding operator(extensions.<=>) v_emb))::numeric, 3) as confidence
    from public.scars s
    where s.embedding is not null and (1 - (s.embedding operator(extensions.<=>) v_emb)) >= p_threshold
    order by s.embedding operator(extensions.<=>) v_emb
    limit 5
  ) h;

  if v_hits is null then
    v_price := p_base;
  else
    v_top_sim := (v_hits->0->>'confidence')::double precision;
    v_top_conf := (v_hits->0->>'confirmations')::int;
    v_price := least(p_max, p_base + 0.0015 * (v_top_sim * ln((v_top_conf + 1.7))));
  end if;
  v_price := round(v_price, 6);

  select balance_credits into v_balance from public.accounts where id = p_account for update;
  if v_balance is null then
    return json_build_object('ok', false, 'error', 'unknown_account');
  end if;
  if v_balance < v_price then
    return json_build_object('ok', false, 'error', 'insufficient_funds', 'required_credits', v_price, 'balance_credits', v_balance);
  end if;
  update public.accounts set balance_credits = balance_credits - v_price where id = p_account;
  insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id) values (p_account, -v_price, 'scar_check', v_ref);

  return json_build_object('ok', true, 'price_charged_credits', v_price,
    'balance_credits', round(v_balance - v_price, 6), 'hits', coalesce(v_hits, '[]'::json));
end; $$;

-- Submit a failure: pay for novel scars, charge a sliver for dupes. Atomic.
create or replace function public.scar_submit(p_account uuid, p_action text, p_sig_sha text, p_failure_mode text, p_context jsonb, p_evidence text, p_embedding text, p_pay numeric, p_dupe_fee numeric)
returns json language plpgsql security definer set search_path = '' as $$
declare v_existing uuid; v_conf int; v_scar uuid; v_balance numeric(14,6); v_ref text;
begin
  select id, confirmations into v_existing, v_conf from public.scars
    where signature_sha = p_sig_sha and lower(failure_mode) = lower(p_failure_mode) for update;

  if v_existing is not null then
    update public.scars set confirmations = confirmations + 1, last_confirmed_at = now()
      where id = v_existing returning confirmations into v_conf;
    v_ref := 'submit_dupe_' || substr(md5(random()::text),1,8);
    select balance_credits into v_balance from public.accounts where id = p_account for update;
    if v_balance >= p_dupe_fee then
      update public.accounts set balance_credits = balance_credits - p_dupe_fee where id = p_account;
      insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id) values (p_account, -p_dupe_fee, 'scar_submit_dupe', v_ref);
      v_balance := v_balance - p_dupe_fee;
    end if;
    return json_build_object('ok', true, 'novel', false, 'scar_id', v_existing, 'confirmations', v_conf, 'price_charged_credits', p_dupe_fee, 'balance_credits', round(v_balance,6));
  end if;

  insert into public.scars(action_signature, signature_sha, failure_mode, context, evidence, embedding, submitted_by)
  values (p_action, p_sig_sha, p_failure_mode, coalesce(p_context,'{}'::jsonb), p_evidence, p_embedding::extensions.vector(384), p_account)
  returning id into v_scar;
  v_ref := 'submit_novel_' || substr(md5(random()::text),1,8);
  update public.accounts set balance_credits = balance_credits + p_pay where id = p_account returning balance_credits into v_balance;
  insert into public.wallet_ledger(account_id, delta_credits, reason, ref_id) values (p_account, p_pay, 'scar_submit_novel', v_ref);
  return json_build_object('ok', true, 'novel', true, 'scar_id', v_scar, 'paid_credits', p_pay, 'balance_credits', round(v_balance,6));
exception when unique_violation then
  select id, confirmations into v_existing, v_conf from public.scars
    where signature_sha = p_sig_sha and lower(failure_mode) = lower(p_failure_mode) for update;
  update public.scars set confirmations = confirmations + 1, last_confirmed_at = now() where id = v_existing returning confirmations into v_conf;
  return json_build_object('ok', true, 'novel', false, 'scar_id', v_existing, 'confirmations', v_conf, 'price_charged_credits', 0, 'balance_credits', (select round(balance_credits,6) from public.accounts where id = p_account));
end; $$;

-- Post a reverse-auction bounty.
create or replace function public.scar_bounty(p_account uuid, p_action text, p_embedding text, p_max_pay numeric, p_ttl int)
returns json language plpgsql security definer set search_path = '' as $$
declare v_id uuid;
begin
  insert into public.bounties(account_id, action_signature, embedding, max_pay_credits, expires_at)
  values (p_account, p_action, p_embedding::extensions.vector(384), p_max_pay, now() + make_interval(secs => p_ttl))
  returning id into v_id;
  return json_build_object('ok', true, 'bounty_id', v_id, 'expires_in_s', p_ttl);
end; $$;

-- Balance + recent ledger.
create or replace function public.scar_wallet(p_account uuid)
returns json language plpgsql security definer set search_path = '' as $$
declare v_balance numeric(14,6); v_ledger json;
begin
  select balance_credits into v_balance from public.accounts where id = p_account;
  select json_agg(l) into v_ledger from (
    select created_at as ts, delta_credits, reason, ref_id
    from public.wallet_ledger where account_id = p_account order by created_at desc limit 10
  ) l;
  return json_build_object('account_id', p_account, 'balance_credits', round(v_balance,6), 'recent_ledger', coalesce(v_ledger,'[]'::json));
end; $$;

revoke execute on function public.scar_register(text,text,text,numeric) from public, anon, authenticated;
revoke execute on function public.scar_auth(text) from public, anon, authenticated;
revoke execute on function public.scar_check(uuid,text,double precision,numeric,numeric) from public, anon, authenticated;
revoke execute on function public.scar_submit(uuid,text,text,text,jsonb,text,text,numeric,numeric) from public, anon, authenticated;
revoke execute on function public.scar_bounty(uuid,text,text,numeric,int) from public, anon, authenticated;
revoke execute on function public.scar_wallet(uuid) from public, anon, authenticated;
grant execute on function public.scar_register(text,text,text,numeric) to service_role;
grant execute on function public.scar_auth(text) to service_role;
grant execute on function public.scar_check(uuid,text,double precision,numeric,numeric) to service_role;
grant execute on function public.scar_submit(uuid,text,text,text,jsonb,text,text,numeric,numeric) to service_role;
grant execute on function public.scar_bounty(uuid,text,text,numeric,int) to service_role;
grant execute on function public.scar_wallet(uuid) to service_role;
