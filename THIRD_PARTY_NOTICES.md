# Sources and third-party components

BandBox Studio is an independent project. It is not affiliated with or endorsed by JBL or Harman. JBL, BandBox, and product/model names belong to their respective owners and identify compatible hardware or controls.

The repository contains the project's implementation and protocol descriptions. It does not distribute JBL One, an APK, firmware, decompiled app source, or device captures. The MIT license applies to this project's original source and documentation; it does not grant rights to third-party software or trademarks.

## Protocol references

- [OpenJBL protocol notes](https://github.com/NiceDayZc/openjbl/blob/e6111667eaab8ee5816345605ac212f8d7d14725/docs/PROTOCOL.md), by the OpenJBL contributors, provided a reference for JBL's BLE service and Protocol 4 framing. Its portable-speaker commands are not treated as proof of BandBox compatibility.
- [JBL One](https://storage.harman.com/release/jblone/appqrcode/jbloneglobal.html) versions 2.7.9 and 2.1.16 were examined to understand BandBox command formats and UI parameter mappings. Protocol knowledge was implemented in the project's own Python and Swift code. No proprietary app code is included.
- [JBL BandBox Trio product information](https://www.jbl.com/BANDBOX-TRIO.html) identifies the target device.

## Runtime dependencies

Dependencies are installed separately, with their own licenses. Direct dependencies include:

| Component | Use | Upstream license |
| --- | --- | --- |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | Local stdio MCP server | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE) |
| [Starlette](https://github.com/Kludex/starlette) | Local GUI HTTP application | [BSD 3-Clause](https://github.com/Kludex/starlette/blob/main/LICENSE.md) |
| [Uvicorn](https://github.com/Kludex/uvicorn) | Local HTTP server | [BSD 3-Clause](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) |
| [HTTPX](https://github.com/encode/httpx) | HTTP test client | [BSD 3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) |

Python, Swift, and Apple's platform frameworks remain subject to their respective licenses. Check installed packages for the full dependency and license information. The browser interface uses local HTML, CSS, and JavaScript without an external asset CDN.

## OpenJBL license notice

The following notice is retained for the OpenJBL reference material that informed transport framing. Its [upstream license](https://github.com/NiceDayZc/openjbl/blob/main/LICENSE) is MIT:

```text
MIT License

Copyright (c) 2026 OpenJBL contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
