# Quarry Python API

Use `quarry` as a thin client of the Quarry daemon. The package re-exports
`ClientConfig`, `QuarryClient`, `TargetResolver`, `TaskOutcome`, and the client
error hierarchy. Request and response models live in `quarry.api`.
Engine modules are server-side implementation details, not the client API.

## Package

::: quarry
    options:
      members: false

## Client and errors

::: quarry.client.ClientConfig

::: quarry.client.HttpError

::: quarry.client.QuarryClient

::: quarry.client.QuarryConnectionError

::: quarry.client.QuarryError

::: quarry.client.TargetResolver

::: quarry.client.TaskOutcome

## Request and response models

::: quarry.api

::: quarry.api.API_VERSION
    options:
      show_if_no_docstring: true
