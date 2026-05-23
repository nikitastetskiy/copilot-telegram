# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-XX-XX

Initial public release.

### Added

- macOS Terminal window watcher that forwards Copilot CLI interactive prompts to Telegram.
- Selection / yes-no / text-input prompt detection with bilateral pipe-border guard.
- Inline Telegram keyboards for selecting menu options or replying.
- `--dump` flag for offline screenshot debugging (no Telegram credentials required).
- `--pick` interactive picker for choosing the Terminal window to watch.
- `--version` / `-V` flag.
- Fixture-based test suite (pytest) covering parser, keyboard builder, and injection logic.
- MIT license; CI workflow with SHA-pinned actions.
