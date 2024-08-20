import os

# TODO: This should be applied on paths in the configuration file loader instead of later in the process
class FilepathReconciler:
    def __init__(self, configuration_file_directory: str, base_dir: str = '') -> None:
        self._config_file_dir = configuration_file_directory
        self._base_dir = base_dir        

    def set_base_dir(self, base_dir: str) -> None:
        self._base_dir = base_dir

    def reconcile_filepath(self, filepath: str) -> str:
        # check the current working directory from where Orca was called to find the file
        filepath = os.path.join(self._base_dir, filepath)
        cwd_filepath =  os.path.join(os.getcwd(), filepath)
        
        if os.path.isfile(cwd_filepath):
            return cwd_filepath
        
        # if it doesn't exist in the current working directory, check the directory from which the configuration was loaded
        config_root_filepath = os.path.join(self._config_file_dir, filepath)
        if os.path.isfile(config_root_filepath):
            return config_root_filepath
        
        # else throw an error of file not found
        raise FileNotFoundError(f"File does not exist at either {cwd_filepath} nor {config_root_filepath}")
        