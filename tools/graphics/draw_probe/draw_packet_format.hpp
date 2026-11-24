#pragma once
#include <d3d11_4.h>
#include <wrl/client.h>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

// Little-endian Win64 D3D11 descriptors, never COM pointers. Versioned diagnostic interchange.
namespace dp {
using Microsoft::WRL::ComPtr;
constexpr size_t limit = 160u * 1024 * 1024;
constexpr uint32_t magic = 0x31504454;
inline void need(bool valid, const char *reason) { if (!valid) throw std::runtime_error(reason); }
inline void hr(HRESULT value, const char *reason) { need(SUCCEEDED(value), reason); }

struct writer {
    std::string data;
    void raw(const void *p, size_t n) {
        need(n <= limit - data.size(), "packet_byte_budget");
        if (n) data.append(static_cast<const char *>(p), n);
    }
    template<class T> void put(const T &v) {
        static_assert(std::is_trivially_copyable_v<T>);
        raw(&v, sizeof(v));
    }
    void blob(const void *p, size_t n) { need(n <= limit,"blob_budget"); put(uint32_t(n)); raw(p,n); }
};
struct reader {
    const char *p; size_t left;
    const char *take(size_t n) {
        need(n <= left, "truncated_packet"); auto *start = p; p += n; left -= n; return start;
    }
    template<class T> T get() {
        static_assert(std::is_trivially_copyable_v<T>); T v; std::memcpy(&v,take(sizeof(v)),sizeof(v)); return v;
    }
    std::string blob() { auto n=get<uint32_t>(); return std::string(take(n),n); }
};
struct header {
    uint32_t signature = magic, version = 1;
    LUID adapter{};
    D3D_FEATURE_LEVEL level{};
    UINT sequence{}, width{}, height{}, x{}, y{}, w{}, h{};
    UINT indexed{}, count{}, instances{}, first{}; INT base{}; UINT firstInstance{};
    D3D11_PRIMITIVE_TOPOLOGY topology{};
    D3D11_RASTERIZER_DESC2 raster{};
    D3D11_BLEND_DESC1 blend{};
    FLOAT factors[4]{}; UINT mask{};
    D3D11_DEPTH_STENCIL_DESC depth{};
    UINT stencilRef{}, viewportCount{}, scissorCount{};
    D3D11_VIEWPORT viewports[16]{};
    D3D11_RECT scissors[16]{};
};
struct element { char name[96]{}; UINT index{}, fmt{}, slot{}, offset{}, step{}; };
inline UINT unit_bytes(DXGI_FORMAT f, bool &bc) {
    bc=false;
    switch (f) {
    case DXGI_FORMAT_BC1_TYPELESS: case DXGI_FORMAT_BC1_UNORM: case DXGI_FORMAT_BC1_UNORM_SRGB:
    case DXGI_FORMAT_BC4_TYPELESS: case DXGI_FORMAT_BC4_UNORM: case DXGI_FORMAT_BC4_SNORM: bc=true; return 8;
    case DXGI_FORMAT_BC2_TYPELESS: case DXGI_FORMAT_BC2_UNORM: case DXGI_FORMAT_BC2_UNORM_SRGB:
    case DXGI_FORMAT_BC3_TYPELESS: case DXGI_FORMAT_BC3_UNORM: case DXGI_FORMAT_BC3_UNORM_SRGB:
    case DXGI_FORMAT_BC5_TYPELESS: case DXGI_FORMAT_BC5_UNORM: case DXGI_FORMAT_BC5_SNORM:
    case DXGI_FORMAT_BC7_TYPELESS: case DXGI_FORMAT_BC7_UNORM: case DXGI_FORMAT_BC7_UNORM_SRGB: bc=true; return 16;
    default:
        if (f >= DXGI_FORMAT_R32G32B32A32_TYPELESS && f <= DXGI_FORMAT_R32G32B32A32_SINT) return 16;
        if (f >= DXGI_FORMAT_R32G32B32_TYPELESS && f <= DXGI_FORMAT_R32G32B32_SINT) return 12;
        if (f >= DXGI_FORMAT_R16G16B16A16_TYPELESS && f <= DXGI_FORMAT_R32G32_SINT) return 8;
        if (f >= DXGI_FORMAT_R10G10B10A2_TYPELESS && f <= DXGI_FORMAT_X24_TYPELESS_G8_UINT) return 4;
        if (f >= DXGI_FORMAT_R8G8_TYPELESS && f <= DXGI_FORMAT_R16_SINT) return 2;
        if (f >= DXGI_FORMAT_R8_TYPELESS && f <= DXGI_FORMAT_A8_UNORM) return 1;
        if (f == DXGI_FORMAT_B8G8R8A8_UNORM || f == DXGI_FORMAT_B8G8R8A8_TYPELESS || f == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) return 4;
        throw std::runtime_error("texture_format_unsupported");
    }
}
inline void shape(const D3D11_TEXTURE2D_DESC &d, UINT mip, UINT &pitch, UINT &rows) {
    need(d.Width && d.Height && d.Width<=8192 && d.Height<=8192 && d.ArraySize==1 && d.MipLevels &&
         d.MipLevels <= 15 && mip<d.MipLevels && d.SampleDesc.Count==1 && d.SampleDesc.Quality==0,"unsupported_texture_shape");
    bool bc=false; auto unit=unit_bytes(d.Format,bc);
    UINT w=d.Width >> mip, h=d.Height >> mip;
    if (!w) w=1;
    if (!h) h=1;
    pitch=(bc ? (w+3)/4 : w)*unit; rows=bc ? (h+3)/4 : h;
}
constexpr UINT end=UINT_MAX;
}
