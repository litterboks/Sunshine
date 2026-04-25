/**
 * @file tests/unit/platform/linux/test_kmsgrab.cpp
 * @brief Unit tests for src/platform/linux/kmsgrab.cpp helpers.
 */
// SUNSHINE_BUILD_DRM gates linkage: kmsgrab.cpp (which defines the symbol
// we forward-declare below) is only compiled when libdrm is available.
#if defined(SUNSHINE_BUILD_DRM)

  #include "../../../tests_common.h"

  #include <optional>
  #include <string_view>
  #include <utility>
  #include <xf86drmMode.h>

namespace platf::kms {
  // Forward declarations; symbols are made externally linkable in test
  // builds via the SUNSHINE_TESTS guard in kmsgrab.cpp. The pair members
  // are (DRM connector type, 1-based instance index).
  std::optional<std::pair<std::uint32_t, std::uint32_t>> parse_connector_name(std::string_view name);
  int resolve_output_name(const std::string &output_name);
}  // namespace platf::kms

using platf::kms::parse_connector_name;
using platf::kms::resolve_output_name;

// pair members are (DRM connector type, 1-based instance index).

TEST(ParseConnectorName, AcceptsKnownTypes) {
  auto dp = parse_connector_name("DP-2");
  ASSERT_TRUE(dp.has_value());
  EXPECT_EQ(dp->first, static_cast<std::uint32_t>(DRM_MODE_CONNECTOR_DisplayPort));
  EXPECT_EQ(dp->second, 2u);

  auto dp_alias = parse_connector_name("DisplayPort-3");
  ASSERT_TRUE(dp_alias.has_value());
  EXPECT_EQ(dp_alias->first, static_cast<std::uint32_t>(DRM_MODE_CONNECTOR_DisplayPort));
  EXPECT_EQ(dp_alias->second, 3u);

  auto hdmi = parse_connector_name("HDMI-A-1");
  ASSERT_TRUE(hdmi.has_value());
  EXPECT_EQ(hdmi->first, static_cast<std::uint32_t>(DRM_MODE_CONNECTOR_HDMIA));
  EXPECT_EQ(hdmi->second, 1u);

  auto edp = parse_connector_name("eDP-1");
  ASSERT_TRUE(edp.has_value());
  EXPECT_EQ(edp->first, static_cast<std::uint32_t>(DRM_MODE_CONNECTOR_eDP));
  EXPECT_EQ(edp->second, 1u);
}

TEST(ParseConnectorName, AcceptsLargeIndices) {
  auto big = parse_connector_name("DP-99");
  ASSERT_TRUE(big.has_value());
  EXPECT_EQ(big->second, 99u);
}

TEST(ParseConnectorName, RejectsOverflowingIndices) {
  // UINT32_MAX + 1 (= 2^32) doesn't fit in uint32_t.
  EXPECT_FALSE(parse_connector_name("DP-4294967296").has_value());
  // More than 10 decimal digits — caught by size guard before from_view
  // even runs.
  EXPECT_FALSE(parse_connector_name("DP-12345678901").has_value());
  EXPECT_FALSE(parse_connector_name("DP-99999999999999999999").has_value());
}

TEST(ParseConnectorName, RejectsZeroIndex) {
  // DRM connector indices are 1-based; "DP-0" is not a valid name.
  EXPECT_FALSE(parse_connector_name("DP-0").has_value());
}

TEST(ParseConnectorName, RejectsMalformedInput) {
  EXPECT_FALSE(parse_connector_name("").has_value());
  EXPECT_FALSE(parse_connector_name("DP").has_value());  // no dash
  EXPECT_FALSE(parse_connector_name("DP-").has_value());  // empty suffix
  EXPECT_FALSE(parse_connector_name("-1").has_value());  // empty prefix
  EXPECT_FALSE(parse_connector_name("DP-foo").has_value());  // non-numeric suffix
  EXPECT_FALSE(parse_connector_name("DP-1a").has_value());  // mixed suffix
  EXPECT_FALSE(parse_connector_name("DP- 1").has_value());  // whitespace
}

TEST(ParseConnectorName, RejectsUnknownConnectorType) {
  EXPECT_FALSE(parse_connector_name("XYZ-1").has_value());
  EXPECT_FALSE(parse_connector_name("foo-1").has_value());
}

TEST(ParseConnectorName, AcceptsKernelUnknownPrefix) {
  // The kernel emits "Unknown%u-N" for connector types libdrm doesn't have a
  // canonical name for. kms::from_view's existing handling parses the raw
  // numeric type out of "Unknown%u", so connector names of that shape are
  // round-trippable. Document the behavior so a future tightening of
  // from_view doesn't silently break this case.
  auto unknown = parse_connector_name("Unknown42-3");
  ASSERT_TRUE(unknown.has_value());
  EXPECT_EQ(unknown->first, 42u);
  EXPECT_EQ(unknown->second, 3u);
}

// resolve_output_name's numeric path doesn't depend on card_descriptors
// state, so we can exercise it directly in unit tests.
TEST(ResolveOutputName, EmptyReturnsZero) {
  // Documented "Sunshine will select the default display" behavior.
  EXPECT_EQ(resolve_output_name(""), 0);
}

TEST(ResolveOutputName, AcceptsNumericIndex) {
  EXPECT_EQ(resolve_output_name("0"), 0);
  EXPECT_EQ(resolve_output_name("1"), 1);
  EXPECT_EQ(resolve_output_name("42"), 42);
}

TEST(ResolveOutputName, RejectsOutOfRangeNumeric) {
  // INT_MAX has 10 digits (2147483647); 11+ digits is rejected by the
  // size guard before util::from_view runs.
  EXPECT_EQ(resolve_output_name("12345678901"), -1);
  // 10-digit values above INT_MAX are caught by the post-parse range check.
  EXPECT_EQ(resolve_output_name("4294967296"), -1);
}

#endif  // SUNSHINE_BUILD_DRM
