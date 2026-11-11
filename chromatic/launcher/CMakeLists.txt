# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
cmake_minimum_required(VERSION 3.19)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/..")
# Recorded in the app description, so the PC tool can name the installed version.
# Keep equal to PJ_LAUNCHER_VERSION in main/launcher_main.c.
set(PROJECT_VER "1.5.1")
# On-console Wi-Fi / Jellyfin setup (docs/Design-notes.md). 0: compiled out entirely, the
# launcher writes no flash. 1: setup_esp32.c and the audited provision_write.c are built in. Flip here only.
set(PJ_ENABLE_SETUP 1)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(chromatic_launcher)

# IDF writes an app to the FIRST app slot, which here is the stock firmware at 0x10000.
# So every stock flash target must fail. Install only from the composed write plan.
add_custom_target(refuse_idf_flash
    COMMAND ${CMAKE_COMMAND} -E echo "REFUSED: idf.py flash would overwrite the stock firmware at 0x10000."
    COMMAND ${CMAKE_COMMAND} -E false)
function(refuse_flash_targets)
    # Some targets only exist once IDF finishes configuring, so this runs last.
    foreach(flash_target flash app-flash bootloader-flash partition-table-flash erase_flash erase-flash erase_otadata
            encrypted-flash encrypted-app-flash encrypted-bootloader-flash encrypted-partition-table-flash)
        if(TARGET ${flash_target})
            add_dependencies(${flash_target} refuse_idf_flash)
        endif()
    endforeach()
endfunction()
cmake_language(DEFER DIRECTORY ${CMAKE_SOURCE_DIR} CALL refuse_flash_targets)
