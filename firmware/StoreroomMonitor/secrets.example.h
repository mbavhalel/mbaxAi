#pragma once
#include "TrustedRoots.h"

// Copy this file to secrets.h in this SAME sketch folder and fill it in.
// secrets.h is ignored by Git. Do not use a Supabase service-role key here.
namespace DeviceConfig {
constexpr char WIFI_SSID[] = "";
constexpr char WIFI_PASSWORD[] = "";
constexpr char INGEST_URL[] = "https://uijggknglvidlyakhupx.supabase.co/functions/v1/ingest-device";
// The same randomly generated DEVICE_TOKEN configured in Supabase secrets.
constexpr char DEVICE_TOKEN[] = "";

// Public CA roots supplied with this project; TLS verification stays enabled.
// If your project's issuer is not covered, replace this with its trusted root
// PEM (or an updated verified root bundle), never with setInsecure().
constexpr const char* ROOT_CA = STOREROOM_PUBLIC_ROOTS;

// Inventory mode sends every tag, including unknown tags for registration.
// Set true only if the single authorisedUID card should reach Supabase.
// This flag controls inventory uploads; no physical door lock is operated.
constexpr bool SEND_ONLY_AUTHORIZED_TAGS = false;
}  // namespace DeviceConfig
