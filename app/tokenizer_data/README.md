# Bundled local tokenizer

`gemma3.model` is Google's cleaned Gemma 3 SentencePiece vocabulary, the same
binary referenced by the Google Gen AI Python SDK's local tokenizer loader.

- Upstream: https://github.com/google/gemma_pytorch
- Source revision: `014acb7ac4563a5f77c76d7ff98f31b568c16508`
- Source path: `tokenizer/gemma3_cleaned_262144_v2.spiece.model`
- SHA-256: `1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c`
- Upstream repository license: Apache-2.0; the full license is in `LICENSE`.

The vocabulary is unmodified. Bundling it makes counting offline and reproducible;
the adapter performs no runtime downloads or token-count API calls.

The Cookie channel uses it as a **local text/tool estimate**, not as a claim that
every current/future Gemini model shares this tokenizer or that hidden reasoning,
media, private protocol overhead, failed attempts and billed usage are observable.
