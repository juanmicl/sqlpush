# Security policy

sqlpush plans and applies DDL against your database. If you found a way
it could apply something it shouldn't, corrupt data, or otherwise put a
schema at risk, please report it privately.

## Reporting

Use GitHub's private vulnerability reporting on this repository
("Report a vulnerability" under the Security tab). Include the sqlpush
version, the command you ran, the models involved where possible, and
what you expected versus what happened.

You should hear back within a few days.

## Scope

In scope:

- Anything that applies destructive DDL despite the destructive gate
- Advisory lock or transaction behavior that could interleave two
  pushers
- Rendering or classification bugs that mislabel risk

Out of scope: bugs in alembic, SQLAlchemy, psycopg, or the database
itself. Report those upstream.
