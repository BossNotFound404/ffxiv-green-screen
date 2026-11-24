#pragma once

namespace fan_inventory {

// Original vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx.
inline constexpr char avfx_sha256[] = "a3877e010b18f29fdbdfca7164a6450daf6856f84d4b4e5ecd65f0b4eff37a58";

// The enum values are the source .atex formats. BC3 is stored as DXT5 blocks.
enum class storage { bc1 = 0x3420, a8 = 0x1131, bc3 = 0x3431 };
enum class mode { screen, add, blend };

struct texture {
    const char *name;
    unsigned width, height;
    const char *hash;
    storage format;
    unsigned mip0_bytes;
    unsigned mip_levels, mip_chain_bytes;
    const char *mip_chain_hash;
};

// Source AVFX texture order. Paths are vfx/monster/gimmick3/texture/<name>.atex.
// Hashes cover tightly packed mip bytes in source order, excluding the .atex header.
inline constexpr texture fingerprints[] = {
    {"ele017bm_a1", 256, 256,
     "dba2b890615918d25b479ea03187f6a2fd52b1c0f45ff9e2f67320d4bb749069", storage::bc1, 32768, 9, 43704,
     "6416256f4039f5e28f4d71ca17c48098036a9f52650f01b0cc96b28cdc76ceac"},
    {"glow601_i_a1", 64, 64,
     "0ad1fe332781973e2a9240b7e7c0b93e7d43d8275277f5f3706766001886b065", storage::a8, 4096, 7, 5461,
     "5019ceeb5bb6508db7d319a74c0ce827493fe53bc679d772e32e25fc1437ab10"},
    {"glow001s_a1", 32, 32,
     "671f5f8e900ca67343a987781be8de3e28a97887553d0957b65eddbb47d4af08", storage::a8, 1024, 6, 1365,
     "7f3ae6ba074d004fd905d5a03e8827b20bae8331fed0a8d38c7a82b9416815fb"},
    {"tone013_c_a1", 256, 8,
     "a064602ef8ef135f00a8057cce3543ae3bba92358b292a07ea873d5ac3258906", storage::bc3, 2048, 9, 3088,
     "7f39ddadaa7950ea323668277c445b19e8b793b9eeacbaaf5025c57e05ab069a"},
    {"ele020bm_a1", 256, 128,
     "bf2fb1cfb02c9f3df89fda430bd86b15564acf1f2b37b9aa4b9b09c0fd637eef", storage::bc1, 16384, 9, 21864,
     "9f4d68b90f4a39c1c9ca717dc75611e80023c8bbf0e49f015fc3227e5056032a"},
    {"uv_fire_hikkaki_a1", 256, 256,
     "95a18c2297d261d3bd2b2f8c3ac5b4a941f857ddc4d3e4cca4f46aff82f71cff", storage::bc1, 32768, 9, 43704,
     "0c5f5a212a0f561b2bcc152466e5d89214420588f4e85a55be3d86ef7e604fd3"},
    {"rd_elc_uv_008gr_i_a1", 256, 256,
     "11b5a7858c4c23be994e0d2d81fcfb033de442bd723e4d14c7ff47f9de81f72e", storage::bc1, 32768, 9, 43704,
     "d1e88aab6132a0de1846367c648d21a08289502bac230e4791bebdda3e938e39"},
};

struct particle {
    unsigned definition;
    mode draw_mode;
    unsigned texture_mask;
};

// Masks contain textures in enabled authored slots. These signatures provide
// candidates; the shader, blend factors and same-frame pixels determine actual use.
inline constexpr particle particles[] = {
    {0, mode::add, 0x02},    // Windmill, TC1 glow601_i_a1
    {1, mode::screen, 0x02}, // Windmill, TC1 glow601_i_a1
    {2, mode::add, 0x04},    // Powder, TC1 glow001s_a1
    {3, mode::blend, 0x08},  // DecalRing, TC1 + TC2 tone013_c_a1
    {4, mode::screen, 0x10}, // LightModel, TC1 ele020bm_a1
    {5, mode::screen, 0x10},
    {6, mode::screen, 0x10},
    {7, mode::screen, 0x10},
    {8, mode::screen, 0x10},
    {9, mode::add, 0x20},    // DecalRing, TD uv_fire_hikkaki_a1
};

} // namespace fan_inventory
