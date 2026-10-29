-- Run against a migrated disposable database as postgres. Everything rolls back.
-- Requires auth.uid() (provided by Supabase; local tests can stub this function).
\set ON_ERROR_STOP on
begin;
insert into auth.users(id) values('11111111-1111-4111-8111-111111111111'),('22222222-2222-4222-8222-222222222222') on conflict do nothing;
insert into public.members(user_id) values('11111111-1111-4111-8111-111111111111');

set local role anon;
do $$ begin
  begin perform public.get_state(); raise exception 'Anonymous get_state was allowed'; exception when insufficient_privilege then null; end;
  begin perform 1 from public.products; raise exception 'Anonymous table access was allowed'; exception when insufficient_privilege then null; end;
  begin perform public.ingest_device('{"type":"climate","t":22,"h":50}'); raise exception 'Anonymous ingest was allowed'; exception when insufficient_privilege then null; end;
end $$;
reset role;
set local role authenticated;
set local request.jwt.claim.sub = '22222222-2222-4222-8222-222222222222';
do $$ begin
  if public.is_member() then raise exception 'Unenrolled user is a member'; end if;
  begin perform public.get_state(); raise exception 'Nonmember get_state was allowed'; exception when insufficient_privilege then null; end;
  begin perform public.manage_stock('clear','{}'); raise exception 'Nonmember mutation was allowed'; exception when insufficient_privilege then null; end;
  if exists(select 1 from public.settings) then raise exception 'Nonmember RLS leaked settings'; end if;
  if exists(select 1 from public.change_signal) then raise exception 'Nonmember RLS leaked update signals'; end if;
end $$;
reset role;
set local role authenticated;
set local request.jwt.claim.sub = '11111111-1111-4111-8111-111111111111';
do $$ begin
  if not public.is_member() then raise exception 'Enrolled user denied'; end if;
  perform public.get_state();
  begin insert into public.products(uid,name,stock,low) values('01:02:03:04','Bypass',1,0); raise exception 'Direct browser writes were allowed'; exception when insufficient_privilege then null; end;
  begin perform public.ingest_device('{"type":"climate","t":22,"h":50}'); raise exception 'Member used service-only ingest'; exception when insufficient_privilege then null; end;
  begin perform public.claim_alerts('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'); raise exception 'Member claimed service-only alerts'; exception when insufficient_privilege then null; end;
end $$;
select public.manage_stock('add','{"uid":"04a1b2c3","name":"Apples","stock":2,"low":1}');
do $$ begin
  if (select version from public.change_signal where singleton) < 1 then raise exception 'Stock change did not signal dashboard'; end if;
end $$;
do $$ begin
  begin perform public.manage_stock('add','{"uid":"01020304","name":"Bad","stock":1.5}'); raise exception 'Fractional stock accepted'; exception when invalid_parameter_value then null; end;
  begin perform public.manage_stock('add','{"uid":"01020304","name":"Bad","stock":null}'); raise exception 'Null stock accepted'; exception when invalid_parameter_value then null; end;
  begin perform public.manage_stock('settings','{"tMin":"NaN","tMax":25,"hMin":30,"hMax":60}'); raise exception 'NaN accepted'; exception when invalid_parameter_value then null; end;
end $$;
reset role;

set local role service_role;
select public.ingest_device('{"type":"scan","uid":"04A1B2C3","event_id":"33333333-3333-4333-8333-333333333333"}');
select public.ingest_device('{"type":"scan","uid":"04A1B2C3","event_id":"33333333-3333-4333-8333-333333333333"}');
do $$ begin
  if (select stock from public.products where uid='04:A1:B2:C3') <> 1 then raise exception 'Duplicate scan decremented stock twice'; end if;
  if (select count(*) from public.scans where uid='04:A1:B2:C3') <> 1 then raise exception 'Duplicate scan logged twice'; end if;
  if (select count(*) from public.alerts where cancelled_at is null and delivered_at is null) <> 1 then raise exception 'Low-stock alert not queued once'; end if;
end $$;
select public.ingest_device('{"type":"scan","uid":"04A1B2C3","event_id":"44444444-4444-4444-8444-444444444444"}');
select public.ingest_device('{"type":"scan","uid":"04A1B2C3","event_id":"55555555-5555-4555-8555-555555555555"}');
do $$ begin
  if (select stock from public.products where uid='04:A1:B2:C3') <> 0 then raise exception 'Stock did not floor at zero'; end if;
  if (select count(*) from public.alerts where cancelled_at is null and delivered_at is null and level=2) <> 1 then raise exception 'Out-of-stock warning missing or duplicated'; end if;
end $$;
select public.ingest_device('{"type":"climate","t":22.5,"h":48}');
do $$ begin
  begin perform public.ingest_device('{"type":"scan","uid":"04A1B2C3"}'); raise exception 'Missing idempotency key accepted'; exception when invalid_parameter_value then null; end;
  begin perform public.ingest_device('{"type":"climate","t":"Infinity","h":48}'); raise exception 'Infinite climate accepted'; exception when invalid_parameter_value then null; end;
  begin perform public.ingest_device('{"type":"climate","t":22,"h":101}'); raise exception 'Invalid humidity accepted'; exception when invalid_parameter_value then null; end;
end $$;
-- A second worker cannot claim an unexpired lease.
select id from public.claim_alerts('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
do $$ begin
  if exists(select 1 from public.claim_alerts('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')) then raise exception 'An alert was claimed twice'; end if;
end $$;
reset role;

set local role authenticated;
set local request.jwt.claim.sub = '11111111-1111-4111-8111-111111111111';
do $$ declare state jsonb; begin
  state := public.get_state();
  if state->'climate'->>'t' <> '22.5' then raise exception 'State climate incorrect'; end if;
  if state->>'device' <> 'online' then raise exception 'Device status incorrect'; end if;
  if jsonb_array_length(public.get_history(24)) < 1 then raise exception 'History empty'; end if;
end $$;
select public.manage_stock('update','{"uid":"04:A1:B2:C3","stock":10}');
do $$ begin
  if exists(select 1 from public.alerts where cancelled_at is null and delivered_at is null) then raise exception 'Restock left stale alerts'; end if;
end $$;
select public.manage_stock('settings','{"tMin":12,"tMax":27,"hMin":25,"hMax":65}');
select public.manage_stock('clear','{}');
do $$ begin
  if (select count(*) from public.products) <> 0 then raise exception 'Clear left products'; end if;
  if (select count(*) from public.scans) <> 0 then raise exception 'Clear left scans'; end if;
  if (select count(*) from public.readings) < 1 then raise exception 'Clear removed climate history'; end if;
  if (public.get_state()->'settings'->>'tMin')::numeric <> 12 then raise exception 'Settings did not persist'; end if;
end $$;
reset role;
do $$ begin
  if exists(select 1 from pg_publication_tables where pubname='supabase_realtime' and schemaname='public' and tablename in ('products','readings','scans','ignored','settings','device_state','alerts')) then raise exception 'Inventory tables must not be in realtime publication'; end if;
  if exists(select 1 from pg_publication where pubname='supabase_realtime') and not exists(select 1 from pg_publication_tables where pubname='supabase_realtime' and schemaname='public' and tablename='change_signal') then raise exception 'Dashboard signal is not published'; end if;
end $$;
rollback;
