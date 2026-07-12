/*
 * Endless Sky DataFile syntax witness for GameForge external-case evidence.
 *
 * Derived from Endless Sky's DataFile/DataNode/Utf8 implementation at commit
 * b10b7d6c24496e2f67a230a2553b344e200ba289. This standalone source is
 * distributed under GPL-3.0-or-later; see ../LICENSE.upstream.txt and
 * source-provenance.json. It validates syntax and reports counts. It is not the
 * complete Endless Sky engine or a semantic reference checker.
 */

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

namespace {

struct Counts {
	std::size_t files = 0;
	std::size_t nodes = 0;
	std::size_t tokens = 0;
};

bool IsContinuation(unsigned char value)
{
	return (value & 0xC0U) == 0x80U;
}

bool IsValidUtf8(const std::string &data)
{
	for(std::size_t i = 0; i < data.size();)
	{
		const auto first = static_cast<unsigned char>(data[i]);
		if(first <= 0x7FU)
		{
			++i;
			continue;
		}
		std::size_t width = 0;
		std::uint32_t codepoint = 0;
		std::uint32_t minimum = 0;
		if((first & 0xE0U) == 0xC0U)
		{
			width = 2;
			codepoint = first & 0x1FU;
			minimum = 0x80U;
		}
		else if((first & 0xF0U) == 0xE0U)
		{
			width = 3;
			codepoint = first & 0x0FU;
			minimum = 0x800U;
		}
		else if((first & 0xF8U) == 0xF0U)
		{
			width = 4;
			codepoint = first & 0x07U;
			minimum = 0x10000U;
		}
		else
			return false;
		if(i + width > data.size())
			return false;
		for(std::size_t offset = 1; offset < width; ++offset)
		{
			const auto next = static_cast<unsigned char>(data[i + offset]);
			if(!IsContinuation(next))
				return false;
			codepoint = (codepoint << 6U) | (next & 0x3FU);
		}
		if(codepoint < minimum || codepoint > 0x10FFFFU
			|| (codepoint >= 0xD800U && codepoint <= 0xDFFFU))
			return false;
		i += width;
	}
	return true;
}

bool IsSpace(unsigned char value)
{
	return value <= 0x20U;
}

bool ParseFile(const std::string &path, Counts &counts)
{
	std::ifstream input(path, std::ios::binary);
	if(!input)
	{
		std::cerr << path << ": unable to open input\n";
		return false;
	}
	const std::string data(
		(std::istreambuf_iterator<char>(input)),
		std::istreambuf_iterator<char>());
	if(!IsValidUtf8(data))
	{
		std::cerr << path << ":1: invalid UTF-8 input\n";
		return false;
	}
	if(data.find('\0') != std::string::npos)
	{
		std::cerr << path << ":1: NUL byte is not allowed\n";
		return false;
	}

	std::vector<std::size_t> indentation;
	std::size_t line = 1;
	std::size_t start = 0;
	while(start < data.size())
	{
		const std::size_t newline = data.find('\n', start);
		std::size_t end = newline == std::string::npos ? data.size() : newline;
		if(end > start && data[end - 1] == '\r')
			--end;
		std::size_t pos = start;
		while(pos < end && IsSpace(static_cast<unsigned char>(data[pos])))
			++pos;
		const std::size_t indent = pos - start;
		if(pos < end && data[pos] != '#')
		{
			++counts.nodes;
			while(!indentation.empty() && indentation.back() >= indent)
				indentation.pop_back();
			indentation.push_back(indent);

			while(pos < end)
			{
				while(pos < end && IsSpace(static_cast<unsigned char>(data[pos])))
					++pos;
				if(pos >= end || data[pos] == '#')
					break;
				const char quote = data[pos];
				if(quote == '"' || quote == '`')
				{
					++pos;
					while(pos < end && data[pos] != quote)
						++pos;
					if(pos >= end)
					{
						std::cerr << path << ':' << line
							<< ": unterminated quoted token\n";
						return false;
					}
					++pos;
				}
				else
				{
					while(pos < end && !IsSpace(static_cast<unsigned char>(data[pos])))
						++pos;
				}
				++counts.tokens;
			}
		}
		if(newline == std::string::npos)
			break;
		start = newline + 1;
		++line;
	}
	++counts.files;
	return true;
}

} // namespace

int main(int argc, char **argv)
{
	if(argc < 2)
	{
		std::cerr << "usage: endless-sky-data-parser <file>...\n";
		return 2;
	}
	Counts counts;
	for(int i = 1; i < argc; ++i)
	{
		if(!ParseFile(argv[i], counts))
			return 2;
	}
	std::cout << "files=" << counts.files
		<< " nodes=" << counts.nodes
		<< " tokens=" << counts.tokens << '\n';
	return 0;
}
