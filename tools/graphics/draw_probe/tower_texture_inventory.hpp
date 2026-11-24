#pragma once

// Original vfx/omen/eff/m0562_share_trap_o0g2.avfx and referenced mip-zero fingerprints.
// Source AVFX SHA-256: fd8134478447e64a3c63536ef156cddbfe7094e36fe3c273442a01bc9d985661
// Fingerprint order starts with the four original lab identities.
namespace tower_inventory {

enum class storage { bc1, a8 };
enum class mode { screen, add, blend };

struct texture {
    const char *name;
    unsigned width, height;
    const char *hash;
    storage format;
    unsigned mip0_bytes;
};

inline constexpr texture fingerprints[] = {
    {"bh_uv_moyo4t", 256, 256,
     "d0deb77d977d344924e95a8c40cd63684d147e2b13ead6e648b61eb823353043", storage::bc1, 32768},
    {"bh_grd02s", 128, 128,
     "2ff2aa8466455a8f52e0339c6773cd5e01db878f20bf4f11c3cfaa161784cce9", storage::bc1, 8192},
    {"bh_uv_cryst1s", 128, 128,
     "2016a13bf0eef470a485130928e429d87932b846af6cea246d647af2f5c47c5d", storage::bc1, 8192},
    {"m0057_uv_line1s", 256, 128,
     "1d8b905d802bf8e4f907e380e1081eaa09b9b0f383af3474b8eb87681ad1de49", storage::bc1, 16384},
    {"glow001bf", 128, 128,
     "aec27356b8943a1abc942459f9e6b47e360a0931ce7b84b5ead5102e14569a28", storage::a8, 16384},
    {"othr070_c", 128, 128,
     "76b2da8649ac37a1e0e21e9f41d8fb39d673ed56de766a778836e5bd62fcff11", storage::a8, 16384},
    {"lstm01as", 128, 128,
     "d376e99d11a07103b17622723527304547db8937d3282a6971737de28fda19ea", storage::bc1, 8192},
    {"bh_grd01s", 64, 128,
     "e28d63621f159c79a54ea1ec3c82884b94225c7c8b7aa5edb6ec2c785cecdf57", storage::bc1, 4096},
    {"bh_grd04s", 128, 128,
     "1f0ba5c1acf1aab67488dc417ee17752e04585647767da5f2acdf2e536d7b3f1", storage::bc1, 8192},
    {"uv_grd01as", 32, 256,
     "3a2c11374660481737b39357f4984eaa3a50bc8b6d0bb9a78681165db0ff3f4e", storage::bc1, 4096},
};

struct particle {
    unsigned definition;
    mode draw_mode;
    unsigned texture_mask;
};

// Masks contain all distinct textures in that definition's enabled authored slots.
// Unambiguous matching also depends on the live shader, blend, and instance evidence.
inline constexpr particle particles[] = {
    {0, mode::add, 0x0c0}, // lstm01as, bh_grd01s
    {1, mode::screen, 0x101}, // bh_uv_moyo4t, bh_grd04s
    {2, mode::screen, 0x007}, // bh_uv_moyo4t, bh_grd02s, bh_uv_cryst1s
    {3, mode::add, 0x00a}, // bh_grd02s, m0057_uv_line1s
    {4, mode::add, 0x040}, // lstm01as
    {5, mode::blend, 0x084}, // bh_uv_cryst1s, bh_grd01s
    {6, mode::blend, 0x101}, // bh_uv_moyo4t, bh_grd04s
    {7, mode::add, 0x300}, // bh_grd04s, uv_grd01as
    {8, mode::blend, 0x020}, // othr070_c
    {9, mode::blend, 0x020}, // othr070_c
    {10, mode::blend, 0x020}, // othr070_c
    {11, mode::blend, 0x010}, // glow001bf
    {12, mode::blend, 0x020}, // othr070_c
    {13, mode::blend, 0x020}, // othr070_c
    {14, mode::add, 0x010}, // glow001bf
    {15, mode::screen, 0x010}, // glow001bf
    {16, mode::screen, 0x010}, // glow001bf
    {17, mode::add, 0x010}, // glow001bf
};

} // namespace tower_inventory
