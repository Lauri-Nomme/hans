#!/usr/bin/env python3
"""Lab-only hack: force SetupController.hasLicenseAndBaseUrl() to return true.

The Bitbucket 9.4.18 setup wizard gates every step behind
`hasLicenseAndBaseUrl()` (== licenseService.isPresent() && baseUrl != null),
so unattended setup cannot complete without a DC license. This patcher rebuilds
the method's Code attribute as `iconst_1; ireturn` (return true) with no stack
map table / branch targets, so it passes JVM verification.

Usage: patch-setup.py <SetupController.class> <output.class>
"""
import sys


def read_u2(b, off):
    return int.from_bytes(b[off:off + 2], "big")


def read_u4(b, off):
    return int.from_bytes(b[off:off + 4], "big")


def parse_cp(data):
    off = 8
    cp_count = read_u2(data, off)
    off += 2
    cp = [None] * cp_count
    i = 1
    while i < cp_count:
        tag = data[off]
        off += 1
        if tag == 1:  # Utf8
            length = read_u2(data, off)
            off += 2
            cp[i] = ("Utf8", data[off:off + length].decode("utf-8", "replace"))
            off += length
        elif tag in (7, 8, 16, 19, 20):  # Class, String, MethodType, Module, Package
            cp[i] = (tag, data[off:off + 2])
            off += 2
        elif tag == 15:  # MethodHandle
            cp[i] = (tag, data[off:off + 3])
            off += 3
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):  # int/float/refs/NameAndType/Dyn
            cp[i] = (tag, data[off:off + 4])
            off += 4
        elif tag in (5, 6):  # Long, Double take two slots
            cp[i] = (tag, data[off:off + 8])
            off += 8
            i += 1
            cp[i] = ("used", None)
        else:
            raise SystemExit(f"unknown constant pool tag {tag}")
        i += 1
    return cp, off


def main():
    src, dst = sys.argv[1], sys.argv[2]
    data = bytearray(open(src, "rb").read())
    cp, off = parse_cp(data)

    # Skip access_flags, this_class, super_class, interfaces.
    off += 6
    ifc_count = read_u2(data, off)
    off += 2 + 2 * ifc_count
    # Skip fields.
    f_count = read_u2(data, off)
    off += 2
    for _ in range(f_count):
        off += 6
        n_attr = read_u2(data, off)
        off += 2
        for _ in range(n_attr):
            length = read_u4(data, off + 2)
            off += 6 + length

    # Methods.
    m_count = read_u2(data, off)
    off += 2
    code_attr_start = None
    code_attr_end = None
    for _ in range(m_count):
        _, name_i, desc_i = read_u2(data, off), read_u2(data, off + 2), read_u2(data, off + 4)
        off += 6
        n_attr = read_u2(data, off)
        off += 2
        for _ in range(n_attr):
            a_name = cp[read_u2(data, off)][1]
            length = read_u4(data, off + 2)
            attr_start = off
            attr_end = off + 6 + length
            if (cp[name_i][1] == "hasLicenseAndBaseUrl" and cp[desc_i][1] == "()Z"
                    and a_name == "Code"):
                code_attr_start = attr_start
                code_attr_end = attr_end
            off = attr_end

    if code_attr_start is None:
        raise SystemExit("hasLicenseAndBaseUrl Code attribute not found")

    name_index = read_u2(data, code_attr_start)
    # New Code attribute: max_stack=1, max_locals=1, code={iconst_1,ireturn},
    # no exception table, no attributes.
    new_attr = bytearray()
    new_attr += name_index.to_bytes(2, "big")
    new_attr += (14).to_bytes(4, "big")   # attribute_length
    new_attr += (1).to_bytes(2, "big")    # max_stack
    new_attr += (1).to_bytes(2, "big")    # max_locals
    new_attr += (2).to_bytes(4, "big")    # code_length
    new_attr += b"\x04\xac"               # iconst_1 ; ireturn
    new_attr += (0).to_bytes(2, "big")    # exception_table_length
    new_attr += (0).to_bytes(2, "big")    # attributes_count

    old_len = code_attr_end - code_attr_start
    out = bytearray(data[:code_attr_start]) + new_attr + data[code_attr_end:]
    print(f"patched hasLicenseAndBaseUrl: old Code attr {old_len}B -> {len(new_attr)}B")
    with open(dst, "wb") as f:
        f.write(out)
    print(f"wrote {dst}")


if __name__ == "__main__":
    main()