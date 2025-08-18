 #ifndef __FILESYSTEM_SAFE_H__
 #define __FILESYSTEM_SAFE_H__
 
 #if __has_include(<filesystem>)
    #include <filesystem>
    namespace fs = std::filesystem;
    
    inline fs::path LexicallyNormal(const fs::path& p) 
    {
        return p.lexically_normal();
    }

 #elif __has_include(<experimental/filesystem>)
    #include <experimental/filesystem>
    namespace fs = std::experimental::filesystem;

    inline std::string LexicallyNormal(const std::string& path) 
    {
        // Split the path into components
        std::vector<std::string> components;
        size_t start = 0;
        size_t end = path.find('/');
        while (end != std::string::npos) {
            components.push_back(path.substr(start, end - start));
            start = end + 1;
            end = path.find('/', start);
        }
        components.push_back(path.substr(start));

        // Process the components to remove "." and ".."
        std::vector<std::string> result;
        for (const auto& component : components) {
            if (component == ".." && !result.empty()) {
                result.pop_back();
            } else if (component != "." && !component.empty()) {
                result.push_back(component);
            }
        }

        // Assemble the normalized path
        std::string normalized_path = "/";
        for (const auto& component : result) {
            normalized_path += component + "/";
        }

        // Remove the trailing "/"
        if(normalized_path.empty() == false && (path.back() != '/' && path.back() != '.'))
        {
            normalized_path.pop_back();
        }

        return normalized_path;
    }
 #endif

 #endif