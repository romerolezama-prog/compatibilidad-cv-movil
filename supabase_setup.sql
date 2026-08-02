insert into storage.buckets (id, name, public)
values ('cv-postulacion', 'cv-postulacion', false)
on conflict (id) do update set public = false;
