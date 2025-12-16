# Agent Instructions for GitHub Copilot

This document provides instructions for AI agents (including GitHub Copilot) working on this repository.

The Nine Articles of Development

The constitution defines nine articles that shape every aspect of the development process:

Article I: Library-First Principle

Every feature must begin as a standalone library—no exceptions. This forces modular design from the start:

Every feature  MUST begin its existence as a standalone library.
No feature shall be implemented directly within application code without
first being abstracted into a reusable library component.

This principle ensures that specifications generate modular, reusable code rather than monolithic applications. When the LLM generates an implementation plan, it must structure features as libraries with clear boundaries and minimal dependencies.

Article II: CLI Interface Mandate

Every library must expose its functionality through a command-line interface:

All CLI interfaces MUST:
- Accept text as input (via stdin, arguments, or files)
- Produce text as output (via stdout)
- Support JSON format for structured data exchange

This enforces observability and testability. The LLM cannot hide functionality inside opaque classes—everything must be accessible and verifiable through text-based interfaces.

Article III: Test-First Imperative

The most transformative article—no code before tests:

This is NON-NEGOTIABLE: All implementation MUST follow strict Test-Driven Development.
No implementation code shall be written before:
1. Unit tests are written
2. Tests are validated and approved by the user
3. Tests are confirmed to FAIL (Red phase)

This completely inverts traditional AI code generation. Instead of generating code and hoping it works, the LLM must first generate comprehensive tests that define behavior, get them approved, and only then generate implementation.

Articles VII & VIII: Simplicity and Anti-Abstraction

These paired articles combat over-engineering:

Section 7.3: Minimal Project Structure
- Maximum 3 projects for initial implementation
- Additional projects require documented justification

Section 8.1: Framework Trust
- Use framework features directly rather than wrapping them

When an LLM might naturally create elaborate abstractions, these articles force it to justify every layer of complexity. The implementation plan template's "Phase -1 Gates" directly enforce these principles.

Article IX: Integration-First Testing

Prioritizes real-world testing over isolated unit tests:

Tests MUST use realistic environments:
- Prefer real databases over mocks
- Use actual service instances over stubs
- Contract tests mandatory before implementation

This ensures generated code works in practice, not just in theory.
