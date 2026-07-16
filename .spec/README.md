# Project Specifications and Agent Context

This directory is the source of truth for durable project context that helps future agents and maintainers resume work safely.

Keep these files current when changing architecture, external API integrations, environment requirements, or verified operational procedures. Do not put secrets, private keys, credentials, `.env` contents, or personally sensitive data here.

## Contents

- [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md): project goal, code map, current status, integration findings, and next steps.

## Maintenance Rule

Before handing work off, update `PROJECT_CONTEXT.md` when the task changes any of the following:

- external endpoints, response formats, or authentication assumptions;
- repository structure or module responsibilities;
- database setup or run commands;
- completed verification steps, failures, and known limitations.
