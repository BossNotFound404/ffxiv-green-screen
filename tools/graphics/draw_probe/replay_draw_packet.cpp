// Standalone replay. All rendering takes place here, after the capture host exits or resumes.
#define NOMINMAX
#include "draw_packet_format.hpp"
#include <d3dcommon.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string_view>
#include <array>
#include <algorithm>
#include <optional>
namespace fs=std::filesystem;
using namespace dp;
namespace {
// Staging names under a user-selected report directory can exceed MAX_PATH.
fs::path disk_path(const wchar_t *argument) {
    auto name = fs::absolute(argument).lexically_normal().make_preferred().native();
    if (name.rfind(L"\\\\?\\", 0) == 0) return name;
    if (name.rfind(L"\\\\", 0) == 0) return L"\\\\?\\UNC\\" + name.substr(2);
    return L"\\\\?\\" + name;
}
std::string read_file(const fs::path &p) {
    auto n=fs::file_size(p); need(n && n<=limit,"input_size");
    std::ifstream f(p,std::ios::binary); std::string s(size_t(n),'\0');
    need(bool(f.read(s.data(),s.size())),"read_failed"); return s;
}
void save(const fs::path &p,const std::string &s) {
    std::ofstream f(p,std::ios::binary); f.write(s.data(),s.size()); f.close(); need(bool(f),"write_failed");
}
struct replay {
    reader in; header h;
    DXGI_ADAPTER_DESC1 adapter_desc{};
    UINT adapter_index{};
    ComPtr<ID3D11Device> dev; ComPtr<ID3D11DeviceContext> c;
    ComPtr<ID3D11Texture2D> rt,staging;
    ComPtr<ID3D11RenderTargetView> rtv;
    ComPtr<ID3D11DepthStencilView> dsv;
    ComPtr<ID3D11Texture2D> original_depth, private_depth;
    replay(const std::string &packet, std::optional<UINT> explicit_adapter = std::nullopt)
        : in{packet.data(),packet.size()},h(in.get<header>()) {
        need(h.signature==magic && h.version==1 && h.width && h.height && h.width<=8192 && h.height<=8192 &&
             size_t(h.width)*h.height*8<=limit && h.w && h.h && h.w<=512 && h.h<=512 &&
             h.w<=h.width && h.x<=h.width-h.w && h.h<=h.height && h.y<=h.height-h.h &&
             h.viewportCount<=16 && h.scissorCount<=16 && h.count && h.instances,"invalid_header");
        ComPtr<IDXGIFactory1> factory; hr(CreateDXGIFactory1(IID_PPV_ARGS(&factory)),"factory");
        ComPtr<IDXGIAdapter1> selected;
        for (UINT i=0;;++i) {
            ComPtr<IDXGIAdapter1> a;
            const HRESULT enumerated = factory->EnumAdapters1(i, &a);
            if (enumerated == DXGI_ERROR_NOT_FOUND) break;
            hr(enumerated, "enum_adapter");
            DXGI_ADAPTER_DESC1 d{}; hr(a->GetDesc1(&d),"adapter");
            const bool match_luid = d.AdapterLuid.HighPart == h.adapter.HighPart && d.AdapterLuid.LowPart == h.adapter.LowPart;
            if (explicit_adapter ? i == *explicit_adapter : match_luid) {
                selected = a;
                adapter_desc = d;
                adapter_index = i;
                break;
            }
        }
        need(selected != nullptr, explicit_adapter ? "requested_adapter_unavailable" : "captured_adapter_unavailable");
        if (explicit_adapter) need(!(adapter_desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE), "requested_adapter_is_software");
        hr(D3D11CreateDevice(selected.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,&h.level,1,D3D11_SDK_VERSION,&dev,nullptr,&c),"create_device");
        ComPtr<ID3D11Device3> dev3; hr(dev.As(&dev3),"requires_d3d11_device3");
        ComPtr<ID3D11RasterizerState2> rs; hr(dev3->CreateRasterizerState2(&h.raster,&rs),"raster_state");
        ComPtr<ID3D11BlendState1> bs; hr(dev3->CreateBlendState1(&h.blend,&bs),"blend_state");
        ComPtr<ID3D11DepthStencilState> ds; hr(dev->CreateDepthStencilState(&h.depth,&ds),"depth_state");
        c->RSSetState(rs.Get()); c->RSSetViewports(h.viewportCount,h.viewports); c->RSSetScissorRects(h.scissorCount,h.scissors);
        c->OMSetBlendState(bs.Get(),h.factors,h.mask); c->OMSetDepthStencilState(ds.Get(),h.stencilRef);
        c->IASetPrimitiveTopology(h.topology);
        auto elems=in.blob(); need(elems.size()%sizeof(element)==0 && elems.size()/sizeof(element)<=32,"input_layout_size");
        auto vcode=shader(true); shader(false);
        if (!elems.empty()) {
            size_t n=elems.size()/sizeof(element);
            std::vector<element> el(n); std::memcpy(el.data(),elems.data(),elems.size());
            std::vector<D3D11_INPUT_ELEMENT_DESC> desc;
            for (const auto &e:el) {
                need(memchr(e.name,0,sizeof(e.name)) && e.slot<32,"input_element");
                desc.push_back({e.name,e.index,DXGI_FORMAT(e.fmt),e.slot,e.offset,
                                e.step ? D3D11_INPUT_PER_INSTANCE_DATA : D3D11_INPUT_PER_VERTEX_DATA,e.step});
            }
            ComPtr<ID3D11InputLayout> il;
            hr(dev->CreateInputLayout(desc.data(),UINT(n),vcode.data(),vcode.size(),&il),"input_layout");
            c->IASetInputLayout(il.Get());
        }
        for (UINT s=in.get<UINT>();s!=end;s=in.get<UINT>()) {
            need(s<32,"vertex_slot"); auto stride=in.get<UINT>(); auto offset=in.get<UINT>();
            auto b=buffer(D3D11_BIND_VERTEX_BUFFER); auto *ptr=b.Get(); c->IASetVertexBuffers(s,1,&ptr,&stride,&offset);
        }
        if (h.indexed) {
            auto fmt=in.get<DXGI_FORMAT>(); auto offset=in.get<UINT>(); auto b=buffer(D3D11_BIND_INDEX_BUFFER);
            need(fmt==DXGI_FORMAT_R16_UINT || fmt==DXGI_FORMAT_R32_UINT,"index_format"); c->IASetIndexBuffer(b.Get(),fmt,offset);
        }
        if (in.get<UINT>()) {
            const auto vd = in.get<D3D11_DEPTH_STENCIL_VIEW_DESC>();
            original_depth = texture(D3D11_BIND_DEPTH_STENCIL);
            D3D11_TEXTURE2D_DESC depth_desc{};
            original_depth->GetDesc(&depth_desc);
            hr(dev->CreateTexture2D(&depth_desc, nullptr, &private_depth), "private_depth");
            hr(dev->CreateDepthStencilView(private_depth.Get(), &vd, &dsv), "depth_view");
        }
        need(!in.left,"trailing_packet_data");
        D3D11_TEXTURE2D_DESC d{}; d.Width=h.width; d.Height=h.height; d.MipLevels=d.ArraySize=d.SampleDesc.Count=1;
        d.Format=DXGI_FORMAT_R16G16B16A16_FLOAT; d.BindFlags=D3D11_BIND_RENDER_TARGET;
        hr(dev->CreateTexture2D(&d,nullptr,&rt),"target"); hr(dev->CreateRenderTargetView(rt.Get(),nullptr,&rtv),"rtv");
        d.Width=h.w; d.Height=h.h; d.BindFlags=0; d.Usage=D3D11_USAGE_STAGING; d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        hr(dev->CreateTexture2D(&d,nullptr,&staging),"staging");
    }
    ComPtr<ID3D11Buffer> buffer(UINT bind) {
        auto n=in.get<UINT>(); need(n && n<=32*1024*1024,"buffer_size");
        D3D11_BUFFER_DESC d{}; d.BindFlags=bind; d.ByteWidth=n; d.Usage=D3D11_USAGE_IMMUTABLE;
        D3D11_SUBRESOURCE_DATA data{in.take(n),0,0}; ComPtr<ID3D11Buffer> b;
        hr(dev->CreateBuffer(&d,&data,&b),"buffer"); return b;
    }
    ComPtr<ID3D11Texture2D> texture(UINT bind) {
        auto d=in.get<D3D11_TEXTURE2D_DESC>(); need(d.MipLevels && d.MipLevels<=15 && !d.MiscFlags,"texture_desc");
        std::vector<D3D11_SUBRESOURCE_DATA> init;
        for (UINT i=0;i<d.MipLevels;++i) {
            UINT p,rows; shape(d,i,p,rows); init.push_back({in.take(size_t(p)*rows),p,0});
        }
        d.BindFlags=bind; d.Usage=D3D11_USAGE_DEFAULT; d.CPUAccessFlags=0;
        ComPtr<ID3D11Texture2D> tex; hr(dev->CreateTexture2D(&d,init.data(),&tex),"texture"); return tex;
    }
    std::string shader(bool vs) {
        auto code=in.blob(); need(code.size()<=1024*1024,"shader_size");
        if (vs) {
            ComPtr<ID3D11VertexShader> s; hr(dev->CreateVertexShader(code.data(),code.size(),nullptr,&s),"vs");
            c->VSSetShader(s.Get(),nullptr,0);
        } else {
            ComPtr<ID3D11PixelShader> s; hr(dev->CreatePixelShader(code.data(),code.size(),nullptr,&s),"ps");
            c->PSSetShader(s.Get(),nullptr,0);
        }
        for (UINT type=in.get<UINT>();type!=end;type=in.get<UINT>()) {
            UINT slot=in.get<UINT>();
            if (type==D3D_SIT_CBUFFER) {
                need(slot<14,"cb_slot"); auto b=buffer(D3D11_BIND_CONSTANT_BUFFER); auto *p=b.Get();
                if (vs) c->VSSetConstantBuffers(slot,1,&p); else c->PSSetConstantBuffers(slot,1,&p);
            } else if (type==D3D_SIT_TEXTURE) {
                need(slot<128,"srv_slot"); auto vd=in.get<D3D11_SHADER_RESOURCE_VIEW_DESC>(); auto t=texture(D3D11_BIND_SHADER_RESOURCE);
                ComPtr<ID3D11ShaderResourceView> v; hr(dev->CreateShaderResourceView(t.Get(),&vd,&v),"srv"); auto *p=v.Get();
                if (vs) c->VSSetShaderResources(slot,1,&p); else c->PSSetShaderResources(slot,1,&p);
            } else if (type==D3D_SIT_SAMPLER) {
                need(slot<16,"sampler_slot"); auto sd=in.get<D3D11_SAMPLER_DESC>(); ComPtr<ID3D11SamplerState> s;
                hr(dev->CreateSamplerState(&sd,&s),"sampler"); auto *p=s.Get();
                if (vs) c->VSSetSamplers(slot,1,&p); else c->PSSetSamplers(slot,1,&p);
            } else throw std::runtime_error("unknown_binding");
        }
        return code;
    }
    std::string draw(const FLOAT (&bg)[4],const std::string *original=nullptr) {
        // The incoming game draw may write stencil or depth. Each baseline
        // and witness must start from the independent, captured pre-draw data.
        c->OMSetRenderTargets(0, nullptr, nullptr);
        if (private_depth) c->CopyResource(private_depth.Get(), original_depth.Get());
        auto *out = rtv.Get();
        c->OMSetRenderTargets(1, &out, dsv.Get());
        D3D11_BOX box{h.x,h.y,0,h.x+h.w,h.y+h.h,1}; c->ClearRenderTargetView(rtv.Get(),bg);
        if (original) {
            need(original->size()==size_t(h.w)*h.h*8,"native_before_size");
            c->UpdateSubresource(rt.Get(),0,&box,original->data(),h.w*8,0);
        }
        if (h.indexed) c->DrawIndexedInstanced(h.count,h.instances,h.first,h.base,h.firstInstance);
        else c->DrawInstanced(h.count,h.instances,h.first,h.firstInstance);
        c->CopySubresourceRegion(staging.Get(),0,0,0,0,rt.Get(),0,&box);
        D3D11_MAPPED_SUBRESOURCE m{}; hr(c->Map(staging.Get(),0,D3D11_MAP_READ,0,&m),"readback");
        std::string result; result.reserve(size_t(h.w)*h.h*8);
        for (UINT y=0;y<h.h;++y) result.append(static_cast<const char *>(m.pData)+size_t(y)*m.RowPitch,h.w*8);
        c->Unmap(staging.Get(),0); return result;
    }
};

std::string rgb_test_pattern(UINT width, UINT height) {
    // These are exact linear FP16 values for 1/16, 1/8, 1/4, 1/2, 3/4 and 1.
    constexpr uint16_t levels[] = {0x2c00, 0x3000, 0x3400, 0x3800, 0x3a00, 0x3c00};
    constexpr uint16_t one = 0x3c00;
    std::string pixels(size_t(width) * height * 8, '\0');
    for (UINT y = 0; y < height; ++y) {
        for (UINT x = 0; x < width; ++x) {
            const UINT checker = (x / 5 + y / 5) % 2;
            const uint16_t channels[] = {
                levels[(x / 41 + y / 13 + checker) % std::size(levels)],
                levels[(y / 29 + x / 17 + 2 * checker) % std::size(levels)],
                levels[(x / 23 + y / 37 + 3 * checker) % std::size(levels)],
                one,
            };
            std::memcpy(pixels.data() + (size_t(y) * width + x) * 8, channels, sizeof(channels));
        }
    }
    return pixels;
}

std::string read_transmission(const std::string &image) {
    constexpr uint16_t one = 0x3c00;
    std::string alpha(image.size() / 4, '\0');
    for (size_t i = 0; i < image.size(); i += 8) {
        uint16_t bits;
        std::memcpy(&bits, image.data() + i + 6, sizeof(bits));
        need(bits <= one || bits == 0x8000, "invalid_transmission_outside_unit_interval");
        std::memcpy(alpha.data() + i / 4, &bits, sizeof(bits));
    }
    return alpha;
}

void seed_opaque_alpha(std::string &image) {
    constexpr uint16_t one = 0x3c00;
    for (size_t i = 6; i < image.size(); i += 8) {
        std::memcpy(image.data() + i, &one, sizeof(one));
    }
}

enum class witness_mode { alpha, coverage_controls, rgb_controls };

void draw_witness(const fs::path &packet_path, const fs::path &before_path,
                  const fs::path &after_path, const fs::path &output, witness_mode mode,
                  const fs::path &terrain_path = {}, std::optional<UINT> explicit_adapter = std::nullopt) {
    need(!fs::exists(output), "output_directory_already_exists");
    auto packet = read_file(packet_path);
    auto before = read_file(before_path);
    const auto after = read_file(after_path);
    replay r(packet, explicit_adapter);
    const auto &b = r.h.blend.RenderTarget[0];
    const bool coverage = mode != witness_mode::rgb_controls;
    if (coverage) {
        need(!r.h.blend.AlphaToCoverageEnable && b.BlendEnable && !b.LogicOpEnable &&
             b.SrcBlend == D3D11_BLEND_SRC_ALPHA && b.DestBlend == D3D11_BLEND_INV_SRC_ALPHA &&
             b.BlendOp == D3D11_BLEND_OP_ADD && b.SrcBlendAlpha == D3D11_BLEND_ZERO &&
             b.DestBlendAlpha == D3D11_BLEND_INV_SRC_ALPHA && b.BlendOpAlpha == D3D11_BLEND_OP_ADD &&
             b.RenderTargetWriteMask == D3D11_COLOR_WRITE_ENABLE_ALL, "unsupported_coverage_alpha_equation");
    }
    const FLOAT seed[] = {0,0,0,1};
    need(r.draw(seed, &before) == after, "baseline_differs_from_native_after");

    struct control {
        const char *filename;
        std::string bytes;
    };
    std::vector<control> controls;
    auto meta = std::string("{\"status\":\"baseline_exact\",\"method\":\"") +
                (coverage ? "isolated_packet_alpha_one_v1" : "isolated_packet_rgb_controls_v1") +
                "\",\"sequence\":"+std::to_string(r.h.sequence)+",\"width\":"+std::to_string(r.h.w)+",\"height\":"+std::to_string(r.h.h)+
                ",\"x\":"+std::to_string(r.h.x)+",\"y\":"+std::to_string(r.h.y)+",\"targetWidth\":"+std::to_string(r.h.width)+
                ",\"targetHeight\":"+std::to_string(r.h.height)+",\"nativeAfterComparedBytes\":"+std::to_string(after.size());
    std::string alpha;
    if (coverage) {
        seed_opaque_alpha(before);
        const auto result = r.draw(seed, &before);
        for (size_t i = 0; i < result.size(); i += 8) {
            need(std::memcmp(result.data()+i, after.data()+i, 6) == 0, "seeded_witness_rgb_differs_from_native_after");
        }
        alpha = read_transmission(result);
        meta += ",\"witnessRgbComparedBytes\":"+std::to_string(result.size()/8*6)+",\"seedAlpha\":1";
    }

    if (mode != witness_mode::alpha) {
        const FLOAT gray[] = {0.5f, 0.5f, 0.5f, 1};
        const FLOAT white[] = {1, 1, 1, 1};
        const FLOAT green[] = {0, 1, 0, 1};
        const auto pattern = rgb_test_pattern(r.h.w, r.h.h);
        controls = {
            {"black.rgba16f", r.draw(seed)}, {"white.rgba16f", r.draw(white)},
            {"gray.rgba16f", r.draw(gray)}, {"green.rgba16f", r.draw(green)},
            {"pattern.rgba16f", r.draw(seed, &pattern)},
        };
        auto terrain = terrain_path.empty() ? std::string{} : read_file(terrain_path);
        if (!terrain.empty()) {
            need(terrain.size() == before.size(), "terrain_before_size");
            for (size_t i = 0; i < terrain.size(); i += 2) {
                uint16_t channel;
                std::memcpy(&channel, terrain.data() + i, sizeof(channel));
                if (i % 8 != 6) need((channel & 0x7c00) != 0x7c00, "nonfinite_terrain_rgb");
                else if (!coverage) need((channel & 0x7c00) != 0x7c00, "nonfinite_terrain_alpha");
            }
            if (coverage) seed_opaque_alpha(terrain);
            controls.push_back({"terrain.rgba16f", r.draw(seed, &terrain)});
        }
        if (coverage) {
            for (const auto &item : controls) {
                need(read_transmission(item.bytes) == alpha, "coverage_control_alpha_differs_from_seeded_witness");
            }
        }
        controls.push_back({"pattern-before.rgba16f", pattern});
        controls.push_back({"original.rgba16f", after});
        meta += std::string(",\"stage\":\"") + (coverage ? "single_coverage_draw_rt0_linear" : "single_draw_rt0_linear") +
                "\",\"backgroundAlpha\":1,"
                "\"controls\":[\"black\",\"white\",\"gray\",\"green\",\"pattern\"";
        if (!terrain.empty()) {
            controls.push_back({"terrain-before.rgba16f", std::move(terrain)});
            meta += ",\"terrain\"";
        }
        meta += "],\"pattern\":\"rgb_checker_mosaic_v1\"";
        meta += std::string(",\"adapterSelection\":\"") + (explicit_adapter ? "explicit_index" : "captured_luid") +
                "\",\"adapterIndex\":" + std::to_string(r.adapter_index) +
                ",\"capturedAdapterLuid\":[" + std::to_string(r.h.adapter.HighPart) + ',' + std::to_string(r.h.adapter.LowPart) + ']' +
                ",\"replayAdapterLuid\":[" + std::to_string(r.adapter_desc.AdapterLuid.HighPart) + ',' +
                std::to_string(r.adapter_desc.AdapterLuid.LowPart) + "]" +
                ",\"replayVendorId\":" + std::to_string(r.adapter_desc.VendorId) +
                ",\"replayDeviceId\":" + std::to_string(r.adapter_desc.DeviceId);
        if (!coverage) {
            meta += ",\"blend\":{\"enabled\":" + std::string(b.BlendEnable ? "true" : "false") +
                    ",\"src\":" + std::to_string(b.SrcBlend) + ",\"dst\":" + std::to_string(b.DestBlend) +
                    ",\"op\":" + std::to_string(b.BlendOp) + ",\"srcAlpha\":" + std::to_string(b.SrcBlendAlpha) +
                    ",\"dstAlpha\":" + std::to_string(b.DestBlendAlpha) + ",\"opAlpha\":" + std::to_string(b.BlendOpAlpha) +
                    ",\"writeMask\":" + std::to_string(b.RenderTargetWriteMask) + '}';
            if (!terrain_path.empty()) meta += ",\"terrainAlpha\":\"preserved\"";
        }
    }
    if (coverage) controls.push_back({"transmission.r16f", std::move(alpha)});

    need(fs::create_directory(output), "output_directory_already_exists");
    try {
        for (const auto &item : controls) save(output / item.filename, item.bytes);
        save(output/"result.json", meta + "}\n");
    } catch (...) {
        std::error_code ignored;
        fs::remove(output/"result.json", ignored);
        for (const auto &item : controls) fs::remove(output / item.filename, ignored);
        fs::remove(output, ignored);
        throw;
    }
}

UINT parse_adapter_index(std::wstring_view argument) {
    need(!argument.empty() && argument.size() <= 3 &&
         std::all_of(argument.begin(), argument.end(), [](wchar_t digit) { return digit >= L'0' && digit <= L'9'; }),
         "invalid_adapter_index");
    return static_cast<UINT>(std::stoul(std::wstring(argument)));
}
}
int wmain(int argc,wchar_t **argv) {
    try {
        if (argc == 6 && std::wstring_view(argv[1]) == L"--alpha-witness") {
            draw_witness(disk_path(argv[2]), disk_path(argv[3]), disk_path(argv[4]), disk_path(argv[5]), witness_mode::alpha);
            return 0;
        }
        const std::wstring_view option = argc >= 2 ? argv[1] : L"";
        if (option == L"--coverage-controls" || option == L"--rgb-controls") {
            int packet_arg = 2;
            std::optional<UINT> adapter_index;
            if (argc >= 4 && std::wstring_view(argv[2]) == L"--adapter-index") {
                adapter_index = parse_adapter_index(argv[3]);
                packet_arg += 2;
            }
            const int arguments = argc - packet_arg;
            need(arguments == 4 || arguments == 5,
                 "usage: replay_draw_packet (--coverage-controls | --rgb-controls) [--adapter-index N] packet before.rgba16f after.rgba16f NEW_DIRECTORY [terrain.rgba16f]");
            const fs::path terrain = arguments == 5 ? disk_path(argv[packet_arg + 4]) : fs::path{};
            const auto mode = option == L"--coverage-controls" ? witness_mode::coverage_controls : witness_mode::rgb_controls;
            draw_witness(disk_path(argv[packet_arg]), disk_path(argv[packet_arg + 1]),
                         disk_path(argv[packet_arg + 2]), disk_path(argv[packet_arg + 3]),
                         mode, terrain, adapter_index);
            return 0;
        }
        need(argc==5,"usage: replay_draw_packet [--alpha-witness] packet before.rgba16f after.rgba16f NEW_DIRECTORY (or --coverage-controls / --rgb-controls)");
        auto packet=read_file(disk_path(argv[1])), before=read_file(disk_path(argv[2])), after=read_file(disk_path(argv[3]));
        fs::path out=disk_path(argv[4]);
        replay r(packet); const FLOAT black[]{0,0,0,1},gray[]{.5f,.5f,.5f,1},white[]{1,1,1,1},green[]{0,1,0,1};
        auto baseline=r.draw(black,&before);
        need(baseline==after,"baseline_differs_from_native_after");
        need(before!=after,"native_draw_has_no_visible_crop_change");
        auto blackResult=r.draw(black),grayResult=r.draw(gray),whiteResult=r.draw(white),greenResult=r.draw(green);
        need(fs::create_directory(out),"output_directory_already_exists");
        save(out/"original.rgba16f",baseline); save(out/"black.rgba16f",blackResult); save(out/"gray.rgba16f",grayResult);
        save(out/"white.rgba16f",whiteResult); save(out/"green.rgba16f",greenResult);
        save(out/"result.json","{\"status\":\"baseline_exact\",\"stage\":\"single_draw_rt0_linear\",\"sequence\":"+
             std::to_string(r.h.sequence)+",\"width\":"+std::to_string(r.h.w)+",\"height\":"+std::to_string(r.h.h)+
             ",\"nativeAfterComparedBytes\":"+std::to_string(after.size())+",\"backgroundAlpha\":1}\n");
        return 0;
    } catch (const std::exception &e) { std::cerr<<e.what()<<"\n"; return 1; }
}
