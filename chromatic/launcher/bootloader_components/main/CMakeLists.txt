# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
idf_component_register(SRCS "bootloader_start.c"
                    INCLUDE_DIRS "../../shared"
                    REQUIRES bootloader bootloader_support)

idf_build_get_property(target IDF_TARGET)
set(scripts "${IDF_PATH}/components/bootloader/subproject/main/ld/${target}/bootloader.ld"
            "${IDF_PATH}/components/bootloader/subproject/main/ld/${target}/bootloader.rom.ld")
target_linker_script(${COMPONENT_LIB} INTERFACE "${scripts}")
