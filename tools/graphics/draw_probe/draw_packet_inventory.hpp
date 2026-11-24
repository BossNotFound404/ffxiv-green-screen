#pragma once
#include "draw_packet_capture.hpp"
#include <iomanip>
#include <optional>
#include <sstream>

namespace dp {
using resource_slots = std::bitset<D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT>;

template<class T, size_t N, class Get>
std::array<ComPtr<T>, N> bound_objects(Get get) {
    std::array<T *, N> raw{};
    get(raw.data());
    std::array<ComPtr<T>, N> bound;
    for (size_t i = 0; i < N; ++i) bound[i].Attach(raw[i]);
    return bound;
}

inline void describe_buffer(std::ostringstream &out, ID3D11Buffer *buffer) {
    D3D11_BUFFER_DESC d{};
    buffer->GetDesc(&d);
    out << ",\"byteWidth\":" << d.ByteWidth << ",\"miscFlags\":" << d.MiscFlags;
}

inline void describe_resource(std::ostringstream &out, ID3D11Resource *resource) {
    D3D11_RESOURCE_DIMENSION dimension{};
    resource->GetType(&dimension);
    out << ",\"resourceDimension\":" << dimension;
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11Buffer> buffer;
    if (SUCCEEDED(resource->QueryInterface(IID_PPV_ARGS(&texture)))) {
        D3D11_TEXTURE2D_DESC d{};
        texture->GetDesc(&d);
        out << ",\"resourceFormat\":" << d.Format << ",\"width\":" << d.Width << ",\"height\":" << d.Height
            << ",\"arraySize\":" << d.ArraySize << ",\"mipLevels\":" << d.MipLevels
            << ",\"sampleCount\":" << d.SampleDesc.Count << ",\"sampleQuality\":" << d.SampleDesc.Quality
            << ",\"miscFlags\":" << d.MiscFlags;
    } else if (SUCCEEDED(resource->QueryInterface(IID_PPV_ARGS(&buffer)))) {
        describe_buffer(out, buffer.Get());
    }
}

inline void describe_shader_inputs(std::ostringstream &out, ID3D11DeviceContext *context, ID3D11DeviceContext1 *ctx1,
                                   bool vertex, const resource_slots &ps_textures) {
    const auto views = bound_objects<ID3D11ShaderResourceView, D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT>(
        [&](auto **v) {
            if (vertex) context->VSGetShaderResources(0, UINT(ps_textures.size()), v);
            else context->PSGetShaderResources(0, UINT(ps_textures.size()), v);
        });
    out << "{\"shaderResources\":[";
    const char *separator = "";
    for (UINT slot = 0; slot < views.size(); ++slot) {
        if (!views[slot]) continue;
        D3D11_SHADER_RESOURCE_VIEW_DESC view{};
        views[slot]->GetDesc(&view);
        out << separator << "{\"slot\":" << slot << ",\"viewFormat\":" << view.Format
            << ",\"viewDimension\":" << view.ViewDimension;
        if (!vertex) out << ",\"reflectedTextureSlot\":" << ps_textures.test(slot);
        if (view.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE2D)
            out << ",\"mostDetailedMip\":" << view.Texture2D.MostDetailedMip << ",\"viewMipLevels\":" << view.Texture2D.MipLevels;
        ComPtr<ID3D11Resource> resource;
        views[slot]->GetResource(&resource);
        describe_resource(out, resource.Get());
        if (view.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE2D && context->GetResourceMinLOD(resource.Get()) != 0.f)
            out << ",\"nonzeroResourceMinLod\":true";
        out << '}';
        separator = ",";
    }
    out << "],\"constantBuffers\":[";
    constexpr UINT count = D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT;
    std::array<UINT, count> start{}, length{};
    const auto buffers = bound_objects<ID3D11Buffer, count>([&](auto **b) {
        if (ctx1 && vertex) ctx1->VSGetConstantBuffers1(0, count, b, start.data(), length.data());
        else if (ctx1) ctx1->PSGetConstantBuffers1(0, count, b, start.data(), length.data());
        else if (vertex) context->VSGetConstantBuffers(0, count, b);
        else context->PSGetConstantBuffers(0, count, b);
    });
    separator = "";
    for (UINT slot = 0; slot < buffers.size(); ++slot) {
        if (!buffers[slot]) continue;
        out << separator << "{\"slot\":" << slot;
        describe_buffer(out, buffers[slot].Get());
        if (ctx1) out << ",\"firstConstant\":" << start[slot] << ",\"constantCount\":" << length[slot];
        out << '}';
        separator = ",";
    }
    out << "]}";
}

// No copies, maps, or COM handles enter the report. Keep the inventory available even
// when shader bytecode or a resource is unavailable to the packet encoder.
inline std::string input_inventory(ID3D11DeviceContext *context, bool indexed, const resource_slots &ps_textures) {
    std::ostringstream out;
    out << std::boolalpha << '{';
    ComPtr<ID3D11InputLayout> layout;
    context->IAGetInputLayout(&layout);
    std::optional<vertex_slots> used;
    try {
        used = layout ? layout_vertex_slots(metadata(layout.Get(), "input_layout_metadata")) : vertex_slots{};
    } catch (const std::runtime_error &) {}

    constexpr UINT count = D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT;
    std::array<UINT, count> strides{}, offsets{};
    const auto vertices = bound_objects<ID3D11Buffer, count>([&](auto **b) {
        context->IAGetVertexBuffers(0, count, b, strides.data(), offsets.data());
    });
    out << "\"layoutSlotsKnown\":" << used.has_value() << ",\"vertexBuffers\":[";
    const char *separator = "";
    for (UINT slot = 0; slot < vertices.size(); ++slot) {
        if (!vertices[slot]) continue;
        out << separator << "{\"slot\":" << slot << ",\"stride\":" << strides[slot] << ",\"offset\":" << offsets[slot];
        if (used) out << ",\"layoutSlot\":" << used->test(slot);
        describe_buffer(out, vertices[slot].Get());
        out << '}';
        separator = ",";
    }
    out << ']';
    if (indexed) {
        ComPtr<ID3D11Buffer> buffer;
        DXGI_FORMAT format{};
        UINT offset = 0;
        context->IAGetIndexBuffer(&buffer, &format, &offset);
        out << ",\"indexBuffer\":";
        if (buffer) {
            out << "{\"format\":" << format << ",\"offset\":" << offset;
            describe_buffer(out, buffer.Get());
            out << '}';
        } else out << "null";
    }

    ComPtr<ID3D11DeviceContext1> ctx1;
    context->QueryInterface(IID_PPV_ARGS(&ctx1));
    out << ",\"vertexStage\":";
    describe_shader_inputs(out, context, ctx1.Get(), true, ps_textures);
    out << ",\"pixelStage\":";
    describe_shader_inputs(out, context, ctx1.Get(), false, ps_textures);

    ComPtr<ID3D11DepthStencilView> depth;
    context->OMGetRenderTargets(0, nullptr, &depth);
    if (depth) {
        D3D11_DEPTH_STENCIL_VIEW_DESC view{};
        depth->GetDesc(&view);
        out << ",\"depthStencil\":{\"viewFormat\":" << view.Format << ",\"viewDimension\":" << view.ViewDimension
            << ",\"viewFlags\":" << view.Flags;
        if (view.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2D) out << ",\"mipSlice\":" << view.Texture2D.MipSlice;
        ComPtr<ID3D11Resource> resource;
        depth->GetResource(&resource);
        describe_resource(out, resource.Get());
        out << '}';
    }
    out << '}';
    return out.str();
}
}
