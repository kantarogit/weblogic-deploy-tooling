# Copyright (c) 2020, Oracle Corporation and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.
#
# ------------
# Description:
# ------------
#
#   This code prepare a list of models for k8s oeprator
#
#

import java.io.FileOutputStream as JFileOutputStream
import java.io.PrintWriter as JPrintWriter
import os
import sets
import sys
import traceback
from java.lang import System

import oracle.weblogic.deploy.util.PyOrderedDict as OrderedDict
import oracle.weblogic.deploy.util.TranslateException as TranslateException
from oracle.weblogic.deploy.compare import CompareException
from oracle.weblogic.deploy.exception import ExceptionHelper
from oracle.weblogic.deploy.util import CLAException
from oracle.weblogic.deploy.util import PyWLSTException
from oracle.weblogic.deploy.util import VariableException
from oracle.weblogic.deploy.validate import ValidateException
from wlsdeploy.aliases import model_constants
from wlsdeploy.aliases.aliases import Aliases
from wlsdeploy.aliases.location_context import LocationContext
from wlsdeploy.aliases.model_constants import DOMAIN_INFO
from wlsdeploy.aliases.wlst_modes import WlstModes
from wlsdeploy.exception import exception_helper
from wlsdeploy.exception.expection_types import ExceptionType
from wlsdeploy.logging.platform_logger import PlatformLogger
from wlsdeploy.tool.util import filter_helper
from wlsdeploy.tool.util.alias_helper import AliasHelper
from wlsdeploy.tool.util.variable_injector import VariableInjector
from wlsdeploy.tool.validate import validation_utils
from wlsdeploy.tool.validate.validator import Validator
from wlsdeploy.util import cla_helper
from wlsdeploy.util import model
from wlsdeploy.util.cla_utils import CommandLineArgUtil
from wlsdeploy.util.model_context import ModelContext
from wlsdeploy.util.model_translator import FileToPython
from wlsdeploy.util.weblogic_helper import WebLogicHelper
from wlsdeploy.yaml.yaml_translator import PythonToYaml
from wlsdeploy.util import target_configuration_helper

VALIDATION_FAIL=2
PATH_TOKEN='|'
BLANK_LINE=""

_program_name = 'prepareModel'
_class_name = 'prepare_model'
__logger = PlatformLogger('wlsdeploy.prepare_model')

__required_arguments = [
    CommandLineArgUtil.ORACLE_HOME_SWITCH,
    CommandLineArgUtil.MODEL_FILE_SWITCH,
    CommandLineArgUtil.OUTPUT_DIR_SWITCH,
    CommandLineArgUtil.TARGET_SWITCH
]

__optional_arguments = [
    CommandLineArgUtil.VARIABLE_FILE_SWITCH
]

all_changes = []
all_added = []
all_removed = []
compare_msgs = sets.Set()

def __process_args(args, logger):
    """
    Process the command-line arguments.
    :param args: the command-line arguments list
    :raises CLAException: if an error occurs while validating and processing the command-line arguments
    """
    _method_name = '__process_args'

    cla_util = CommandLineArgUtil(_program_name, __required_arguments, __optional_arguments)
    required_arg_map, optional_arg_map = cla_util.process_args(args)

    cla_helper.verify_required_args_present(_program_name, __required_arguments, required_arg_map)

    __process_target_arg(optional_arg_map, required_arg_map, logger)

    combined_arg_map = optional_arg_map.copy()
    combined_arg_map.update(required_arg_map)
    return ModelContext(_program_name, combined_arg_map)

def __process_target_arg(optional_arg_map, required_arg_map, logger):

    _method_name = '__process_target_arg'

    # if -target is specified -output_dir is required
    output_dir = required_arg_map[CommandLineArgUtil.OUTPUT_DIR_SWITCH]
    if output_dir is None or os.path.isdir(output_dir) is False:
        if not os.path.isdir(output_dir):
            ex = exception_helper.create_cla_exception('WLSDPLY-01642', output_dir)
            logger.throwing(ex, class_name=_class_name, method_name=_method_name)
            raise ex

    # Set the -variable_file parameter if not present with default
    if CommandLineArgUtil.VARIABLE_FILE_SWITCH not in optional_arg_map:
        optional_arg_map[CommandLineArgUtil.VARIABLE_FILE_SWITCH] = os.path.join(output_dir,
                                                                                 "k8s_variable.properties")

class PrepareModel:
    """
      This is the main driver for the caller.  It compares two model files whether they are json or yaml format.
    """
    def __init__(self, model_files, model_context, logger, output_dir=None):
        self.model_files = model_files
        self.output_dir = output_dir
        self.model_context = model_context
        aliases = Aliases(model_context=model_context, wlst_mode=WlstModes.OFFLINE)
        self._alias_helper = AliasHelper(aliases, logger, ExceptionType.COMPARE)
        self._logger = logger
        self._name_tokens_location = LocationContext()
        self._name_tokens_location.add_name_token('DOMAIN', "testdomain")
        self.current_dict = None
        self.cache =  OrderedDict()

    def __walk_model_section(self, model_section_key, model_dict, valid_section_folders):
        _method_name = '__validate_model_section'

        if model_section_key not in model_dict.keys():
            return

        # only specific top-level sections have attributes
        attribute_location = self._alias_helper.get_model_section_attribute_location(model_section_key)

        valid_attr_infos = []
        path_tokens_attr_keys = []

        if attribute_location is not None:
            valid_attr_infos = self._alias_helper.get_model_attribute_names_and_types(attribute_location)
            path_tokens_attr_keys = self._alias_helper.get_model_uses_path_tokens_attribute_names(attribute_location)

        model_section_dict = model_dict[model_section_key]
        for section_dict_key, section_dict_value in model_section_dict.iteritems():
            # section_dict_key is either the name of a folder in the
            # section, or the name of an attribute in the section.
            validation_location = LocationContext()

            model_folder_path = model_section_key + ":/"
            if section_dict_key in valid_attr_infos:
                # section_dict_key is the name of an attribute in the section
                self.__walk_attribute(section_dict_key, section_dict_value, valid_attr_infos,
                                      path_tokens_attr_keys, model_folder_path, attribute_location)

            elif section_dict_key in valid_section_folders:
                # section_dict_key is a folder under the model section

                # Append section_dict_key to location context
                validation_location.append_location(section_dict_key)

                # Call self.__validate_section_folder() passing in section_dict_value as the model_node to process
                self.__walk_section_folder(section_dict_value, validation_location)


    def __walk_section_folder(self, model_node, validation_location):
        _method_name = '__validate_section_folder'

        model_folder_path = self._alias_helper.get_model_folder_path(validation_location)

        if self._alias_helper.supports_multiple_mbean_instances(validation_location):

            for name in model_node:
                expanded_name = name

                new_location = LocationContext(validation_location)

                name_token = self._alias_helper.get_name_token(new_location)

                if name_token is not None:
                    new_location.add_name_token(name_token, expanded_name)

                value_dict = model_node[name]

                self.__walk_model_node(value_dict, new_location)

        elif self._alias_helper.requires_artificial_type_subfolder_handling(validation_location):

            for name in model_node:
                expanded_name = name

                new_location = LocationContext(validation_location)

                name_token = self._alias_helper.get_name_token(new_location)

                if name_token is not None:
                    new_location.add_name_token(name_token, expanded_name)

                value_dict = model_node[name]

                self.__walk_model_node(value_dict, new_location)

        else:
            name_token = self._alias_helper.get_name_token(validation_location)

            if name_token is not None:
                name = self._name_tokens_location.get_name_for_token(name_token)

                if name is None:
                    name = '%s-0' % name_token

                validation_location.add_name_token(name_token, name)

            self.__walk_model_node(model_node, validation_location)

    def __walk_model_node(self, model_node, validation_location):
        _method_name = '__process_model_node'

        valid_folder_keys = self._alias_helper.get_model_subfolder_names(validation_location)
        valid_attr_infos = self._alias_helper.get_model_attribute_names_and_types(validation_location)
        model_folder_path = self._alias_helper.get_model_folder_path(validation_location)

        for key, value in model_node.iteritems():

            if key in valid_folder_keys:
                new_location = LocationContext(validation_location).append_location(key)

                if self._alias_helper.is_artificial_type_folder(new_location):
                    # key is an ARTIFICIAL_TYPE folder
                    valid_attr_infos = self._alias_helper.get_model_attribute_names_and_types(new_location)

                    self.__walk_attributes(value, valid_attr_infos, new_location)
                else:
                    self.__walk_section_folder(value, new_location)

            elif key in valid_attr_infos:
                # aliases.get_model_attribute_names_and_types(location) filters out
                # attributes that ARE NOT valid in the wlst_version being used, so if
                # we're in this section of code we know key is a bonafide "valid" attribute
                valid_data_type = valid_attr_infos[key]
                if valid_data_type in ['properties']:
                    valid_prop_infos = {}
                    properties = validation_utils.get_properties(value)
                    self.__walk_properties(properties, valid_prop_infos, validation_location)

                else:
                    path_tokens_attr_keys = \
                        self._alias_helper.get_model_uses_path_tokens_attribute_names(validation_location)

                    self.__walk_attribute(key, value, valid_attr_infos, path_tokens_attr_keys, model_folder_path,
                                          validation_location)

    def __walk_attributes(self, attributes_dict, valid_attr_infos, validation_location):
        _method_name = '__validate_attributes'

        path_tokens_attr_keys = self._alias_helper.get_model_uses_path_tokens_attribute_names(validation_location)

        model_folder_path = self._alias_helper.get_model_folder_path(validation_location)

        for attribute_name, attribute_value in attributes_dict.iteritems():
            self.__walk_attribute(attribute_name, attribute_value, valid_attr_infos, path_tokens_attr_keys,
                                  model_folder_path, validation_location)

    def __walk_attribute(self, attribute_name, attribute_value, valid_attr_infos, path_tokens_attr_keys,
                         model_folder_path, validation_location):
        _method_name = '__validate_attribute'

        if attribute_name in valid_attr_infos:
            expected_data_type = valid_attr_infos[attribute_name]

            if expected_data_type == 'password':
                self.__substitute_password_with_token(model_folder_path, attribute_name, validation_location)

        self._logger.exiting(class_name=_class_name, method_name=_method_name)

    def __walk_properties(self, properties_dict, valid_prop_infos, validation_location):
        _method_name = '__walk_properties'

        for property_name, property_value in properties_dict.iteritems():
            valid_prop_infos[property_name] = validation_utils.get_python_data_type(property_value)
            self.__walk_property(property_name, property_value, valid_prop_infos, validation_location)


    def __walk_property(self, property_name, property_value, valid_prop_infos, model_folder_path, validation_location):

        _method_name = '__walk_property'

        self._logger.entering(property_name, property_value, str(valid_prop_infos), model_folder_path,
                              class_name=_class_name, method_name=_method_name)

        if property_name in valid_prop_infos:
            expected_data_type = valid_prop_infos[property_name]
            if expected_data_type == 'password':
                self.__substitute_password_with_token(model_folder_path, property_name, validation_location)

    def __substitute_password_with_token(self, model_path, attribute_name, validation_location, model_context=None):
        # TODO: move the logic to target_configuration_helper and reuse in variable_injector.process_attribute
        model_path_tokens = model_path.split('/')
        tokens_length = len(model_path_tokens)

        if tokens_length > 1:
            if model_path_tokens[0] == 'domainInfo:' and model_path_tokens[1] == '':
                password_name = "@@SECRET:@@DOAMIN-UID@@-weblogic-credentials:%s@@" % (attribute_name.lower())
                self.cache[attribute_name] = ''
            else:
                password_name = "@@SECRET:@@DOMAIN-UID@@-%s:%s@@" % ('-'.join(model_path_tokens[1:]).lower(),
                                                                            attribute_name.lower())
                self.cache['.'.join(model_path_tokens[1:]) + '-' + attribute_name] = ''

            p_dict = self.current_dict

            for index in range(0, len(model_path_tokens)):
                token = model_path_tokens[index]
                if token == '':
                    break
                if token[-1] == ':':
                    token=token[:-1]
                p_dict = p_dict[token]

            p_dict[attribute_name] = password_name

    def walk(self):

        _method_name = "walk"

        model_file_name = None

        try:

            model_file_list = self.model_files.split(',')
            for model_file in model_file_list:
                if os.path.splitext(model_file)[1].lower() == ".yaml":
                    model_file_name = model_file
                    FileToPython(model_file_name, True).parse()

                aliases = Aliases(model_context=self.model_context, wlst_mode=WlstModes.OFFLINE)

                validator = Validator(self.model_context, aliases, wlst_mode=WlstModes.OFFLINE)

                # Just merge and validate but without substitution
                model_dictionary = cla_helper.merge_model_files(model_file_name, None)

                variable_file = self.model_context.get_variable_file()
                if not os.path.exists(variable_file):
                    variable_file=None

                return_code = validator.validate_in_tool_mode(model_dictionary,
                                                          variables_file_name=variable_file,
                                                          archive_file_name=None)

                if return_code == Validator.ReturnCode.STOP:
                    __logger.severe('WLSDPLY-05705', model_file_name)
                    return VALIDATION_FAIL

                self.current_dict = model_dictionary


                self.__walk_model_section(model.get_model_domain_info_key(), self.current_dict,
                                          aliases.get_model_section_top_level_folder_names(DOMAIN_INFO))

                self.__walk_model_section(model.get_model_topology_key(), self.current_dict,
                                          aliases.get_model_topology_top_level_folder_names())

                self.__walk_model_section(model.get_model_resources_key(), self.current_dict,
                                              aliases.get_model_resources_top_level_folder_names())

                self.current_dict = self._apply_filter_and_inject_variable(self.current_dict, self.model_context,
                                                                           validator)


                file_name = os.path.join(self.output_dir, os.path.basename(model_file_name))
                fos = JFileOutputStream(file_name, False)
                writer = JPrintWriter(fos, True)
                pty = PythonToYaml(self.current_dict)
                pty._write_dictionary_to_yaml_file(self.current_dict, writer)
                writer.close()


        except ValidateException, te:
            __logger.severe('WLSDPLY-20009', _program_name, model_file_name, te.getLocalizedMessage(),
                            error=te, class_name=_class_name, method_name=_method_name)
            ex = exception_helper.create_compare_exception(te.getLocalizedMessage(), error=te)
            __logger.throwing(ex, class_name=_class_name, method_name=_method_name)
            return VALIDATION_FAIL
        except VariableException, ve:
            __logger.severe('WLSDPLY-20009', _program_name, model_file_name, ve.getLocalizedMessage(),
                            error=ve, class_name=_class_name, method_name=_method_name)
            ex = exception_helper.create_compare_exception(ve.getLocalizedMessage(), error=ve)
            __logger.throwing(ex, class_name=_class_name, method_name=_method_name)
            return VALIDATION_FAIL
        except TranslateException, pe:
            __logger.severe('WLSDPLY-20009', _program_name, model_file_name, pe.getLocalizedMessage(),
                            error=pe, class_name=_class_name, method_name=_method_name)
            ex = exception_helper.create_compare_exception(pe.getLocalizedMessage(), error=pe)
            __logger.throwing(ex, class_name=_class_name, method_name=_method_name)
            return VALIDATION_FAIL

        return 0

    def _apply_filter_and_inject_variable(self, model, model_context, validator):
        """
        Customize the model dictionary before persisting. Validate the model after customization for informational
        purposes. Any validation errors will not stop the discovered model to be persisted.
        :param model: completely discovered model
        """
        _method_name = '_apply_filter_and_inject_variable'
        self._logger.entering(class_name=_class_name, method_name=_method_name)

        if filter_helper.apply_filters(model, "discover", model_context):
            self._logger.info('WLSDPLY-06014', _class_name=_class_name, method_name=_method_name)

        variable_injector = VariableInjector(_program_name, model, model_context,
                                             WebLogicHelper(self._logger).get_actual_weblogic_version(), self.cache)
        if self.cache is not None:
            # Generate k8s create secret script, after that clear the dictionary to avoid showing up in the variable file
            if model_context.is_target_k8s():
                validation_method = model_context.get_target_configuration()['validation_method']
                model_context.set_validation_method(validation_method)
                target_configuration_helper.generate_k8s_script(model_context.get_kubernetes_variable_file(), self.cache)
                self.cache.clear()
                if os.path.exists(self.model_context.get_variable_file()):
                    variable_map = validator.load_variables(self.model_context.get_variable_file())
                    self.cache.update(variable_map)

        variables_inserted, return_model, variable_file_location = variable_injector.inject_variables_keyword_file()
        return model

def debug(format_string, *arguments):
    """
    Generic debug code.
    :param format_string:  python formatted string
    :param arguments: arguments for the formatted string
    """
    if os.environ.has_key('DEBUG_COMPARE_MODEL_TOOL'):
        print format_string % (arguments)
    else:
        __logger.finest(format_string, arguments)

def main():
    """
    The main entry point for the discoverDomain tool.
    :param args: the command-line arguments
    """
    _method_name = 'main'

    __logger.entering(class_name=_class_name, method_name=_method_name)
    for index, arg in enumerate(sys.argv):
        __logger.finer('sys.argv[{0}] = {1}', str(index), str(arg), class_name=_class_name, method_name=_method_name)

    _outputdir = None

    try:
        model_context = __process_args(sys.argv, __logger)
        _outputdir = model_context.get_kubernetes_output_dir()
        model1 = model_context.get_model_file()
        for f in [ model1 ]:
            if not os.path.exists(f):
                raise CLAException("Model %s does not exists" % f)
            if os.path.isdir(f):
                raise CLAException("Model %s is a directory" % f)


        obj = PrepareModel(model1, model_context, __logger, _outputdir)
        rc = obj.walk()
        System.exit(rc)

    except CLAException, ex:
        exit_code = 2
        if exit_code != CommandLineArgUtil.HELP_EXIT_CODE:
            __logger.severe('WLSDPLY-20008', _program_name, ex.getLocalizedMessage(), error=ex,
                            class_name=_class_name, method_name=_method_name)
        cla_helper.clean_up_temp_files()
        sys.exit(exit_code)
    except CompareException, ce:
        cla_helper.clean_up_temp_files()
        __logger.severe('WLSDPLY-05704', ce.getLocalizedMessage())
        System.exit(2)
    except PyWLSTException, pe:
        cla_helper.clean_up_temp_files()
        __logger.severe('WLSDPLY-05704', pe.getLocalizedMessage())
        System.exit(2)
    except:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        eeString = traceback.format_exception(exc_type, exc_obj, exc_tb)
        cla_helper.clean_up_temp_files()
        __logger.severe('WLSDPLY-05704', eeString)
        System.exit(2)

def format_message(key, *args):
    """
    Get message using the bundle.
    :param key: bundle key
    :param args: bundle arguments
    :return:
    """
    return ExceptionHelper.getMessage(key, list(args))

if __name__ == "__main__":
    main()


