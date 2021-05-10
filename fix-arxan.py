#!/usr/bin/env python3


'''
Arxan binary fixer using unpacked dump

Authors:	pr701
Contributor:	Jupiter
Version:	1.7.2.0
License:	MIT
'''

import os
import argparse
import pefile
import distorm3

from os.path import isfile
import struct


# Verify executable file type
def exe_file_type(path):
	if not isfile(path):
		raise argparse.ArgumentTypeError('file is not accessible')
	pe = pefile.PE(path, fast_load=True)
	if pe.DOS_HEADER.e_magic != 0x5A4D:
		raise argparse.ArgumentTypeError('the file not executable')
	if pe.FILE_HEADER.Machine != 0x8664 and pe.FILE_HEADER.Machine != 0x014C:
		raise argparse.ArgumentTypeError('unsupported architecture')
	return path


def read_binary(filename):
	data = b''
	with open(filename, 'rb') as f:
		try:
			data = f.read()
		except Exception as e:
			raise
		finally:
			f.close()
	return data


def write_binary(filename, data):
	with open(filename, 'wb') as f:
		try:
			f.write(data)
		except Exception as e:
			raise
		finally:
			f.close()


def strz_to_str(name):
	for i in range(len(name)):
		if name[i] == 0:
			return name[:i].decode("utf-8")
	return name.decode("utf-8")


def is_pe64(pe):
	if pe.FILE_HEADER.Machine == 0x8664:
		return True
	return False


# PE section size without last zeroes
def get_real_section_size(data):
	size = len(data)
	i = size - 1
	while i != 0:
		if data[i] != 0:
			size = i + 1
			break
		i -= 1
	return size


def get_pe_checksum_offset(data):
	e_lfanew_offset = 0x3c
	e_lfanew = struct.unpack('<L', data[e_lfanew_offset:e_lfanew_offset + 4])[0]
	return e_lfanew + 0x58


# PE checksum calculation (slow)
def calculate_pe_checksum(checksum_offset, data):

	# https://github.com/BitsOfBinary

	data_len = len(data)
	checksum = 0
	top = 2 ** 32

	for i in range(0, int(data_len / 4)):
		# Skip CheckSum itself
		if i == int(checksum_offset / 4):
			continue

		dword = struct.unpack('<L', data[i * 4: (i * 4) + 4])[0]
		checksum = (checksum & 0xFFFFFFFF) + dword + (checksum >> 32)

		if checksum > top:
			checksum = (checksum & 0xFFFFFFFF) + (checksum >> 32)

	checksum = (checksum & 0xFFFF) + (checksum >> 16)
	checksum = checksum + (checksum >> 16)
	checksum = checksum & 0xFFFF

	checksum += data_len

	return checksum


class arxan_info:
	present = False
	version = 0
	ep_crypted = True
	text_crypted = True
	access_entry_rva = 0
	access_flag_rva = 0
	decrypt_entry_rva = 0
	build_import_entry_rva = 0
	build_import_flag_rva = 0


# Check arxan entry point
def is_arxan_entry(base, dasm):
	push_entry = 9
	push_count = 0
	for op in dasm:
		if not op.valid:
			return 0
		# entry check
		if op.mnemonic != 'PUSH':
			return 0
		if push_count == push_entry:
			return dasm[0].address - base
		push_count += 1
	return 0


# Check set_access_rwx function
def is_dword_check(base, dasm):
	for op in dasm:
		# selfcheck for crypted
		if op.mnemonic == 'MOV' and len(op.operands) \
			and op.operands[0].type == 'Register' \
			and op.operands[0].index is not None \
			and distorm3.Registers[op.operands[0].index] == 'EAX':
			return op.address + op.size + op.operands[1].disp - base
		if op.mnemonic == 'JMP' or op.mnemonic == 'CALL':
			return 0
	return 0


# Check build_import function
def is_byte_check(base, dasm):
	for op in dasm:
		# selfcheck for crypted
		if op.mnemonic == 'MOVZX' and len(op.operands) \
			and op.operands[0].type == 'Register' \
			and op.operands[0].index is not None \
			and distorm3.Registers[op.operands[0].index] == 'EAX':
			return op.address + op.size + op.operands[1].disp - base
		if op.mnemonic == 'JMP' or op.mnemonic == 'CALL':
			return 0
	return 0


def trace_arxan(pe, addr, condition, skip_condition_count=0, skip_call_count=0, path_file=''):
	max_jmp = 20
	block_cnt = 0

	if args.trace:
		print()
		print('----- TRACING -----')

	arch = distorm3.Decode32Bits
	if is_pe64(pe):
		arch = distorm3.Decode64Bits
	base = pe.OPTIONAL_HEADER.ImageBase

	pe_image = pe.get_memory_mapped_image()
	if addr:
		trace_start = addr
		jmp_count = 0
		valid = True
		while valid:
			if args.trace:
				print()
				block_cnt += 1

				print(f'; {trace_start+base:X} | {trace_start:08X}\n@@{block_cnt:03d}:')

			data = pe_image[trace_start:trace_start + 0xff]
			if not len(data):
				if args.trace:
					print(f'\t; INVALID MEM\t@ {trace_start:X}')
				if len(path_file):
					offset = pe.get_offset_from_rva(trace_start)
					if offset < 0:
						valid = False
						break
					data = read_binary(path_file)[offset:offset + 0xff]
					if not len(data):
						valid = False
						break
					if args.trace:
						print(f'\t; RETRY\t@ {trace_start+base:X} | {trace_start:X}')
				else:
					valid = False
					break
			dasm = distorm3.Decompose(trace_start + base, data, arch)
			rva = condition(base, dasm)
			if rva != 0:
				# condition counter
				if skip_condition_count == 0:
					if args.trace:
						print(f'\tRET\t; {rva+base:X} | {rva:08X}')
					return rva
				if skip_condition_count > 0:
					skip_condition_count -= 1
			for op in dasm:
				# if args.trace:
				if op.valid and args.trace:
					asm_out = str(op).replace(" ", "\t", 1)
					if op.mnemonic == 'JMP':
						print(f'\tJMP\t@@{block_cnt + 1:03d}\t; {op.operands[0].value:X}')
					else:
						print(f'\t{asm_out}')

				# limiter
				if not op.valid or jmp_count > max_jmp:
					if args.trace:
						if not op.valid:
							asm_out = str(op).replace(" ", "\t", 1)
							print(f'\t{asm_out}\t; UNSUPPORTED\n')
						if jmp_count > max_jmp:
							print(f'STOP: Reached jmp limit ({max_jmp})')
					if op.mnemonic == 'DB 0x66': # distorm unsupported instruction, skip
						trace_start = op.address - base + 10
						break
					valid = False
					break
				# arxan v3
				if op.mnemonic == 'LEA' and op.operands[0].type == 'Register' \
					and distorm3.Registers[op.operands[0].index] == 'RBP':
					jmp_count += 1
					trace_start = op.address + op.size + op.operands[1].disp - base
					break
				# next obfuscated node
				if op.mnemonic == 'JMP' or op.mnemonic == 'CALL':
					# call counter
					if op.mnemonic == 'CALL' and skip_call_count > 0:
						skip_call_count -= 1
						continue
					# next node
					jmp_count += 1
					trace_start = op.operands[0].value - base
					break
	return 0


def get_arxan_info(pe, path_file=''):
	arx = arxan_info()
	arx.present = False

	last_section = pe.FILE_HEADER.NumberOfSections - 1
	if strz_to_str(pe.sections[last_section].Name) != '.text':
		return arx

	arch = distorm3.Decode32Bits
	if is_pe64(pe):
		arch = distorm3.Decode64Bits

	# disasm entry
	ep = pe.OPTIONAL_HEADER.AddressOfEntryPoint
	base = pe.OPTIONAL_HEADER.ImageBase
	entry = pe.get_memory_mapped_image()[ep:ep + 0x13]

	entry_point = distorm3.Decompose(base + ep, entry, arch)

	entry_count = len(entry_point)
	if entry_count < 1:
		return arx

	# one of the default entries
	arx.version = 0
	if entry_count >= 2:
		if entry_point[0].valid and entry_point[0].mnemonic == 'SUB' and \
			entry_point[1].valid or entry_point[1].mnemonic == 'CALL':
			arx.version = 1
		if entry_point[0].valid and entry_point[0].mnemonic == 'JMP' and \
			entry_point[1].valid or entry_point[1].mnemonic == 'INT':
			arx.version = 2

	if arx.version == 0:
		return arx
	arx.present = True

	# decrypted entry?
	if len(entry_point) and entry_point[2].valid:
		if arx.version == 1 \
			and entry_point[2].mnemonic == 'ADD' \
			and entry_point[0].operands[1].value == entry_point[2].operands[1].value: # stack restored
			arx.ep_crypted = False
		if arx.version == 2 \
			and entry_point[2].mnemonic == 'CALL': # call init crt
			arx.ep_crypted = False

	# set_access_rwx
	if args.trace:
		print()
		print('Searching for `set_access_rwx` function...')

	arx.access_entry_rva = trace_arxan(pe, ep, is_arxan_entry,
		path_file=path_file)
	if arx.access_entry_rva == 0:
		return arx

	# access_flag
	if args.trace:
		print()
		print_pe_addr('Found `set_access_rwx` function entry @', arx.access_entry_rva, pe)
		print()
		print('Searching for `access_flag` variable...')

	arx.access_flag_rva = trace_arxan(pe, arx.access_entry_rva, is_dword_check,
		path_file=path_file)
	if arx.access_flag_rva == 0:
		return arx

	# decrypt_image
	if args.trace:
		print()
		print_pe_addr('Found `access_flag` variable @', arx.access_flag_rva, pe)
		print()
		print('Searching for `decrypt_image` function...')

	arx.decrypt_entry_rva = trace_arxan(pe, arx.access_entry_rva, is_arxan_entry,
		skip_condition_count=1,
		skip_call_count=1,
		path_file=path_file)
	if arx.decrypt_entry_rva == 0:
		return arx

	# build_import
	if args.trace:
		print()
		print_pe_addr('Found `decrypt_image` function entry @', arx.decrypt_entry_rva, pe)
		print()
		print('Searching for `build_import` function...')

	arx.build_import_entry_rva = trace_arxan(pe, arx.access_entry_rva, is_arxan_entry,
		skip_condition_count=2,
		skip_call_count=2,
		path_file=path_file)

	# arxan obfuscation
	if arx.build_import_entry_rva == arx.decrypt_entry_rva:
		arx.build_import_entry_rva = 0

	if arx.build_import_entry_rva != 0:
		arx.text_crypted = False
		if args.trace:
			print()
			print_pe_addr('Found `build_import` function entry @', arx.build_import_entry_rva, pe)
			print()
			print('Searching for `import_flag` variable...')

		arx.build_import_flag_rva = trace_arxan(pe, arx.build_import_entry_rva, is_byte_check,
			path_file=path_file)
		if arx.build_import_flag_rva != 0:
			if args.trace:
				print()
				print_pe_addr('Found `build_import` variable @', arx.build_import_flag_rva, pe)
				print()

	return arx


def process_arx_dump(exefile, dumpfile, outfile):
	print('Parsing dump file...')
	exe = pefile.PE(exefile, fast_load=True)
	dump = pefile.PE(dumpfile, fast_load=True)

	if not is_pe64(exe):
		raise ValueError('architecture of source binary is not suported. Should be 64 bit PE.')

	print('Tracing...')
	arx_exe = get_arxan_info(exe, path_file=exefile)
	arx_dump = get_arxan_info(dump, path_file=dumpfile)

	if not arx_exe.present or not arx_dump.present:
		raise ValueError('source binary not protected by arxan or unknown version')

	print_summary(exefile, arx_dump, dump)

	print()

	if not arx_exe.text_crypted:
		raise ValueError('source binary entry is not encrypted')

	if arx_dump.text_crypted:
		raise ValueError('dump binary is encrypted, required new dump')

	if arx_exe.access_entry_rva != arx_dump.access_entry_rva or arx_exe.access_flag_rva != arx_dump.access_flag_rva:
		raise ValueError('data in source and dump are different. Aborted.')

	print('Mapping...')
	exe_image = exe.get_memory_mapped_image()
	dump_image = dump.get_memory_mapped_image()

	# check if dump was dumped corrected
	import_flag_byte = struct.unpack('<B', exe_image[arx_exe.build_import_flag_rva:arx_exe.build_import_flag_rva + 1])[0]
	if import_flag_byte == 0:
		raise ValueError('the dump binary was made at the wrong stage')

	print('Processing...')
	out = bytearray(read_binary(exefile))

	section_number = exe.FILE_HEADER.NumberOfSections
	if (section_number != dump.FILE_HEADER.NumberOfSections):
		raise ValueError('the number of sections doesn\'t match')

	# move decrypted data (without validation, take care!)
	for i in range(section_number):
		section = exe.sections[i]
		dump_sec = dump.sections[i]

		# section exist in raw
		if section.PointerToRawData and section.VirtualAddress == dump_sec.VirtualAddress:
			# one of the properties of the packer: a large virtual address by default
			decrypted = dump_image[section.VirtualAddress:section.VirtualAddress + dump_sec.SizeOfRawData]
			# pelib bug workaround
			if len(decrypted) < dump_sec.SizeOfRawData:
				offset = dump.get_offset_from_rva(section.VirtualAddress)
				if offset < 0:
					raise ValueError(f'[error] section {strz_to_str(section.Name)} is out of range')
				decrypted = read_binary(dumpfile)[offset:offset + dump_sec.SizeOfRawData]
				if not len(decrypted):
					raise ValueError(f'[error] section {strz_to_str(section.Name)} is out of file')
			section_size = get_real_section_size(decrypted)
			# support version with crypted section
			if section_size > section.SizeOfRawData:
				print(f'[WARNING]: section {strz_to_str(section.Name)} is larger than original, may be packed.')
			out[section.PointerToRawData:section.PointerToRawData + section.SizeOfRawData] = decrypted[:section.SizeOfRawData]

	# fix init check
	check_offset = exe.get_offset_from_rva(arx_exe.access_flag_rva)
	if check_offset:
		out[check_offset:check_offset + 4] = struct.pack('<L', 0xCCCCCCCC) # 0xDEC0DED0

	# fix import table
	if arx_dump.build_import_flag_rva != 0:
		check_offset = exe.get_offset_from_rva(arx_dump.build_import_flag_rva)
		out[check_offset:check_offset + 1] = struct.pack('<B', 0xCC)

	# restore iat
	print('Restoring IAT...')
	iat_rva = exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IAT']].VirtualAddress
	iat_size = exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IAT']].Size
	iat_off = exe.get_offset_from_rva(iat_rva)
	iat = exe_image[iat_rva:iat_rva + iat_size]
	if len(iat) < iat_size:
		# pelib bugfix
		iat = read_binary(exefile)[iat_off:iat_off + iat_size]
	out[iat_off:iat_off + iat_size] = iat

	# fix config dir data
	cfg_rva = exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG']].VirtualAddress
	cfg_size = exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG']].Size
	if cfg_rva != 0:
		print('Fixing config directory...')
		cfg_size = struct.unpack('<L', exe_image[cfg_rva:cfg_rva + 4])[0]
		# restore security cookie
		if cfg_size > 0x40:
			header = '<2L2H3L6QL2H2Q'
			securityCookie = struct.unpack(header, exe_image[cfg_rva:cfg_rva + struct.calcsize(header)])[17]
			securityCookie -= exe.OPTIONAL_HEADER.ImageBase
			securityOff = exe.get_offset_from_rva(securityCookie)
			out[securityOff:securityOff + 4] = exe_image[securityCookie:securityCookie + 4]

	# remove invalid cert if present
	cert_offset = exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].VirtualAddress
	cert_size = exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].Size
	if cert_offset != 0 and cert_size != 0:
		print('Removing digital signature...')
		exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].VirtualAddress = 0
		exe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].Size = 0

		# write header
		out[0:exe.sections[0].PointerToRawData] = exe.write()[0:exe.sections[0].PointerToRawData]
		out = out[:-cert_size]

	# fix checksum
	if args.fix_header:
		print('Fixing header flags...')
		# restore dll can move flag
		e_lfanew_offset = 0x3c
		e_lfanew = struct.unpack('<L', out[e_lfanew_offset:e_lfanew_offset + 4])[0]
		flag_offset = e_lfanew + 0x5e
		flag = struct.unpack('<H', out[flag_offset:flag_offset + 2])[0]
		flag |= 0x0040
		out[flag_offset:flag_offset + 2] = struct.pack('<H', flag)

		print('Fixing PE checksum...')
		checksum_offset = get_pe_checksum_offset(out)
		checksum = calculate_pe_checksum(checksum_offset, out)
		out[checksum_offset:checksum_offset + 4] = struct.pack('<L', checksum)

	# write
	print('Writing output...')
	write_binary(outfile, out)
	print('Saved to', outfile)


def is_dynamic_base(pe, report=False):
	# Get predefined flag
	image_dynamic_flag = pefile.retrieve_flags(pefile.DLL_CHARACTERISTICS, 'IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE')[0]

	# Check if flag is set
	flag = getattr(pe.OPTIONAL_HEADER, image_dynamic_flag[0], False)
	if report:
		print(f'\tIs Dynamic Base (ASLR): {flag}')
		print_flags('DLL Characteristics', pe.OPTIONAL_HEADER.DllCharacteristics)
		if flag:
			print(f'\t\t{image_dynamic_flag[0]}\n\t\t\t{image_dynamic_flag[1]:X} | {image_dynamic_flag[1]} | {image_dynamic_flag[1]:08b}')
			print('\t\tWARNING: Disable ASLR for dumping')
	return flag


def print_pe_overview(pe):
	print()
	print('PE file overview')
	print('Reported values are in "Hex | Dec | Bin" form')
	print()

	print_addr('Image base', pe.OPTIONAL_HEADER.ImageBase)
	print_size('Size of Image', pe.OPTIONAL_HEADER.SizeOfImage)
	print_dword('CheckSum', pe.OPTIONAL_HEADER.CheckSum)

	is_dynamic_base(pe, report=True)

	print()


# Flag as Hex | Dec | Bin
def print_flags(title, value, group=None):
	if group is not None:
		print(f'{group}:')
	print(f'\t{title}:\t{value:X} | {value} | {value:08b}')


# In square brackets - reversed value (bswap)
def print_dword(title, value, group=None):
	if group is not None:
		print(f'{group}:')
	print(f'\t{title}:\t{value:08X} [{struct.unpack("<I", struct.pack(">I", value))[0]:08X}]')


# Size as Hex | Dec
def print_size(title, size, group=None):
	if group is not None:
		print(f'{group}:')
	print(f'\t{title}:\t{size:X} | {size}')


# Address as Hex
def print_addr(title, addr, group=None):
	if group is not None:
		print(f'{group}:')
	print(f'\t{title}:\t{addr:08X}')


'''
VA	- Virtual Address
RVA	- Relative Virtual Address
RAW	- Raw file offset
'''


# Address: VA, RVA, RAW
def print_pe_addr(title, rva, pe):
	print(f'{title}:')
	print_addr('VA', rva + pe.OPTIONAL_HEADER.ImageBase)
	print_addr('RVA', rva)
	print_addr('RAW', pe.get_offset_from_rva(rva))


def print_summary(file, arx, pe):
	print('Target:', file)
	print('Arxan version:', arx.version)
	print('Crypted code:', arx.text_crypted)
	print()

	print_pe_addr('`set_access_rwx` function entry @', arx.access_entry_rva, pe)
	print_pe_addr('`access_flag` variable @', arx.access_flag_rva, pe)
	print_pe_addr('`decrypt_image` function entry @', arx.decrypt_entry_rva, pe)

	if arx.build_import_entry_rva != 0:
		print_pe_addr('`build_import` function entry @', arx.build_import_entry_rva, pe)
		print_pe_addr('`build_import` variable @', arx.build_import_flag_rva, pe)


def process_arx_info(exefile):
	print('Parsing source PE file...')
	exe = pefile.PE(exefile, fast_load=False)

	if not is_pe64(exe):
		raise ValueError('architecture of source binary is not suported. Should be 64 bit PE.')

	print_pe_overview(exe)

	print('Tracing...')
	arx_exe = get_arxan_info(exe, path_file=exefile)

	if not arx_exe.present:
		raise ValueError('source binary is not protected by arxan or protected by unknown version')

	print_summary(exefile, arx_exe, exe)


# main
def main():
	parser = argparse.ArgumentParser(prog='fix-arxan', description='Arxan binary info extractor/fixer')

	parser.add_argument('-s', '--source', action='store', type=exe_file_type, required=True,
						help='Source executable file packed by arxan')
	parser.add_argument('-d', '--dump', action='store', type=exe_file_type,
						help='Executable dump file with decrypted sections')
	parser.add_argument('-o', '--output', action='store', type=str,
						help='Output merged executable file')
	parser.add_argument('-t', '--trace', action='store_true',
						help='Detailed tracing log')
	parser.add_argument('-f', '--fix-header', action='store_true',
						help='Fix executable headers: checksum and dynamic rebase flag')

	global args
	args = parser.parse_args()

	filename, extension = os.path.splitext(args.source)
	out = filename + '_unp' + extension

	if args.output is not None:
		out = args.output

	try:
		if args.dump is not None:
			process_arx_dump(args.source, args.dump, out)
		else:
			process_arx_info(args.source)
	except Exception as e:
		print('Error: %s' % e)
	else:
		print('Done')


if __name__ == '__main__':
	main()
